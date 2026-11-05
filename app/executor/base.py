"""실행기 인터페이스: 직렬화 가능한 요청·결과와 단일 관문.

모든 실행기(지금은 로컬 mock, 이후 실제/원격)는 `Executor.execute`로만 명령을 실행한다. 서브클래스는 `_run`만 구현하고
`execute`를 재정의할 수 없다(테스트가 확인). `execute`는 항상 이 순서를 거친다.

1. `assert_server_executable(verdict, ...)`: 가드가 허용하지 않은 명령, manual 명령, 승인·확인이 없는 변경은 실행하지 않는다.
2. 요청이 승인된 내용과 같은지 확인한다(마스킹된 승인 문구·종류가 판정과 같고, 실제 명령의 다이제스트가 생성 시점과 같다).
3. 실행하는 쪽에서 가드를 한 번 더 통과시킨다(이중 확인, D-28). 실제 값으로 복원된 명령을 다시 검사한다.
4. `_run`.

실행 위치(이 서버 직접 vs 권한 서버에 요청)와 무관하게 요청은 `to_wire`/`from_wire`로 직렬화된다. 요청의 `command`에는 실제 값이
들어 있으므로 서버·실행기 밖으로 내보내지 않는다(로그·이력·트레이스에는 `masked_command`만).
"""
from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.guards.command_guard import CommandGuard, CommandVerdict
from app.guards.execution_policy import ExecutionDenied, assert_server_executable

MAX_OUTPUT_CHARS = 1_000_000


def _digest(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    session_id: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    kind: Literal["read", "mutate"]
    masked_command: str = Field(max_length=2000)       # 승인된 문구(플레이스홀더 형태). 이력·로그에는 이것만 쓴다
    command: str = Field(max_length=2000)              # 실제 값이 든 명령. 서버·실행기 밖으로 내보내지 않는다
    command_digest: str                                # command의 SHA-256(생성 시점). 승인 후 바뀌었는지 확인한다
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)

    @classmethod
    def create(cls, *, kind: str, masked_command: str, command: str, session_id: str | None = None, plan_id: str | None = None,
               step_id: str | None = None, timeout_seconds: float = 120.0) -> "ExecutionRequest":
        return cls(request_id=uuid.uuid4().hex, session_id=session_id, plan_id=plan_id, step_id=step_id, kind=kind,
                   masked_command=masked_command, command=command, command_digest=_digest(command), timeout_seconds=timeout_seconds)

    def digest_ok(self) -> bool:
        return self.command_digest == _digest(self.command)

    def to_wire(self) -> dict:
        return self.model_dump(mode="json")

    @classmethod
    def from_wire(cls, data: dict) -> "ExecutionRequest":
        return cls.model_validate(data)


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    status: Literal["ok", "failed", "timeout"]
    exit_code: int | None = None
    stdout: str = ""                                   # 원문(실제 값 포함 가능). 모델·이력으로 보내기 전에 마스킹·정리한다
    stderr: str = ""
    duration_seconds: float = 0.0
    mock: bool = False                                 # 실제 AWS가 아니라 mock 환경에서 실행됨
    executor: str = ""
    notes: tuple[str, ...] = ()

    def to_wire(self) -> dict:
        return self.model_dump(mode="json")

    @classmethod
    def from_wire(cls, data: dict) -> "ExecutionResult":
        return cls.model_validate(data)


class Executor(ABC):
    """서브클래스는 `_run`만 구현한다. `execute`는 재정의하지 않는다."""

    name = "executor"
    mock = False

    def __init__(self, guard: CommandGuard):
        self._guard = guard

    def execute(
        self, request: ExecutionRequest, *, verdict: CommandVerdict, approved_by_other: bool = False,
        confirmations: frozenset[str] | set[str] = frozenset(),
    ) -> ExecutionResult:
        assert_server_executable(verdict, approved_by_other=approved_by_other, confirmations=confirmations)
        if verdict.command != request.masked_command or verdict.kind != request.kind:
            raise ExecutionDenied("request_differs_from_approved")
        if not request.digest_ok():
            raise ExecutionDenied("request_tampered")
        again = self._guard.check(request.command)           # 실제 값으로 복원된 명령을 실행하는 쪽에서 다시 검사한다
        try:
            assert_server_executable(again, approved_by_other=approved_by_other, confirmations=confirmations)
        except ExecutionDenied as e:
            raise ExecutionDenied(f"recheck:{e.code}") from None
        if again.kind != request.kind:
            raise ExecutionDenied("recheck:kind_changed")
        return self._run(request)

    @abstractmethod
    def _run(self, request: ExecutionRequest) -> ExecutionResult:
        """가드와 승인을 모두 통과한 요청을 실행한다. 직접 호출하지 않는다."""
