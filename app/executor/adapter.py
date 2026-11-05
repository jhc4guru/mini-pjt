"""에이전트의 읽기 도구(`ReadToolbox`)가 쓰는 `ReadCommandRunner`를 실행기로 구현한다.

도구가 이미 가드를 통과시킨 명령이지만, 어댑터는 다시 가드를 거쳐 판정을 만들고 `Executor.execute`(단일 관문)로만 실행한다.
읽기 명령이 실패해도 예외로 숨기지 않고 오류 출력(종료 코드 포함)을 돌려준다. 도구가 이를 마스킹·시크릿·지시문 제거(`sanitize_untrusted`)
후에 모델에 전달하므로, 모델이 "대상 없음" 같은 실패 원인을 보고 대안을 찾을 수 있다.
"""
from __future__ import annotations

from app.executor.base import ExecutionRequest, Executor
from app.guards.command_guard import CommandGuard
from app.guards.masking import Masker


class ReadRunnerAdapter:
    def __init__(self, executor: Executor, guard: CommandGuard, masker: Masker, *, session_id: str | None = None):
        self._executor, self._guard, self._masker, self._session = executor, guard, masker, session_id

    def run(self, command: str, *, timeout: float) -> str:
        masked = self._masker.mask(command)
        verdict = self._guard.check(masked, self._masker)
        request = ExecutionRequest.create(kind="read", masked_command=masked, command=command, session_id=self._session, timeout_seconds=timeout)
        result = self._executor.execute(request, verdict=verdict)       # 관문을 통과하지 못하면 ExecutionDenied
        if result.status == "ok":
            return result.stdout
        if result.status == "timeout":
            return f"[timeout {timeout:.0f}s]"
        return f"[exit {result.exit_code}] {result.stderr or result.stdout}"[:4000]
