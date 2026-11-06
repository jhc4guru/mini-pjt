"""에이전트 실행 제한: 도구 허용 목록, 호출·시간·토큰 상한, 반복 호출 감지, 구조화 출력 검증.

상한을 넘으면 계속 진행하지 않고 중단한다(BudgetExceeded). 호출한 쪽은 사람에게 넘긴다.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Type

from pydantic import BaseModel, ValidationError

# 에이전트가 쓸 수 있는 도구(전부 읽기 전용). 변경 실행은 승인 엔진을 거치는 별도 경로이고 에이전트 도구가 아니다.
READ_ONLY_TOOLS = frozenset({"search_docs", "lookup_inventory", "run_read_command"})

MAX_PLAN_STEPS = 120
MAX_PHASE_STEPS = 40


class GuardLimitError(RuntimeError):
    """제한 위반의 공통 부모. code는 감사 기록용."""

    code = "limit"
    detail = ""           # 중단 원인을 알아보기 위한 부가 정보(도구 이름 등, 값 없음)


class ToolNotAllowed(GuardLimitError):
    code = "tool_not_allowed"


class BudgetExceeded(GuardLimitError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class RepeatedCall(GuardLimitError):
    """같은 도구를 같은 인자로 상한을 넘겨 호출했다. 처음 `MAX_REPEAT_NOTICES`번은 중단하지 않고 모델에게 오류로 알린다."""
    code = "repeated_call"

    def __init__(self, detail: str = ""):
        super().__init__("repeated_call")
        self.detail = detail


MAX_REPEAT_NOTICES = 2        # 한 번의 실행에서 반복 호출을 알려 주는 횟수. 넘으면 `BudgetExceeded("repeated_call")`로 중단한다(D-45)


class PlanTooLarge(GuardLimitError):
    code = "plan_too_large"


class SchemaViolation(GuardLimitError):
    code = "schema_violation"


@dataclass(frozen=True)
class Limits:
    max_tool_calls: int = 30
    max_steps: int = 40            # 에이전트 추론 단계(LLM 호출) 수
    max_tokens: int = 700_000
    max_seconds: float = 600.0
    max_repeats: int = 2           # 같은 도구·같은 인자 호출을 이 횟수까지만 허용
    # 단계별 예산(D-69, 계획 방식 `batch`에서만 쓴다. `loop`는 위의 `max_tool_calls` 하나를 요청 전체가 공유한다)
    investigation_calls: int = 30  # 조사 단계의 도구 호출
    phase_calls: int = 10          # phase 하나를 쓰는 단계의 보충 조회 호출
    max_total_tool_calls: int = 80  # 단계별 예산이 있어도 요청 전체가 넘을 수 없는 합계


class AgentBudget:
    """에이전트 한 번의 실행(요청 하나)의 예산."""

    def __init__(self, limits: Limits | None = None, allowed_tools: frozenset[str] = READ_ONLY_TOOLS, clock: Callable[[], float] = time.monotonic):
        self.limits = limits or Limits()
        self.allowed_tools = allowed_tools
        self._clock = clock
        self._start = clock()
        self.tool_calls = 0
        self.steps = 0
        self.tokens = 0
        self.repeat_notices = 0
        self._seen: dict[str, int] = {}
        self._staged = False
        self._stage_cap: int | None = None
        self._stage_calls = 0

    def _check_time(self) -> None:
        if self._clock() - self._start > self.limits.max_seconds:
            raise BudgetExceeded("time_exceeded")

    def before_tool(self, name: str, args: Any = None) -> None:
        if name not in self.allowed_tools:
            raise ToolNotAllowed(f"허용되지 않은 도구: {name}")
        self._check_time()
        self.tool_calls += 1
        if self.tool_calls > (self.limits.max_total_tool_calls if self._staged else self.limits.max_tool_calls):
            raise BudgetExceeded("tool_calls_exceeded")
        if self._stage_cap is not None:
            self._stage_calls += 1
            if self._stage_calls > self._stage_cap:
                raise BudgetExceeded("tool_calls_exceeded", detail="stage")
        sig = name + json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
        self._seen[sig] = self._seen.get(sig, 0) + 1
        if self._seen[sig] > self.limits.max_repeats:
            if self.repeat_notices < MAX_REPEAT_NOTICES:         # 알려 주고 한 번 더 기회를 준다(이 호출도 총 호출 수에 들어간다)
                self.repeat_notices += 1
                raise RepeatedCall(name)
            raise BudgetExceeded("repeated_call", detail=name)

    def begin_stage(self, max_calls: int) -> None:
        """단계별 도구 호출 예산을 시작한다(D-69). 이 단계의 호출은 `max_calls`회까지이고, 요청 전체 합계(`max_total_tool_calls`)도 넘을 수 없다.
        토큰·추론 단계·시간·반복 호출 상한은 단계와 무관하게 요청 전체에 적용된다."""
        self._staged = True
        self._stage_cap = max(0, int(max_calls))
        self._stage_calls = 0

    def remaining_tool_calls(self) -> int:
        left = (self.limits.max_total_tool_calls if self._staged else self.limits.max_tool_calls) - self.tool_calls
        if self._stage_cap is not None:
            left = min(left, self._stage_cap - self._stage_calls)
        return max(0, left)

    def before_step(self) -> None:
        self._check_time()
        self.steps += 1
        if self.steps > self.limits.max_steps:
            raise BudgetExceeded("steps_exceeded")

    def add_tokens(self, n: int) -> None:
        self.tokens += max(0, int(n))
        if self.tokens > self.limits.max_tokens:
            raise BudgetExceeded("tokens_exceeded")

    def usage(self) -> dict[str, int | float]:
        return {"tool_calls": self.tool_calls, "steps": self.steps, "tokens": self.tokens, "seconds": round(self._clock() - self._start, 2)}


def check_plan_size(steps_per_phase: list[int], *, max_plan: int = MAX_PLAN_STEPS, max_phase: int = MAX_PHASE_STEPS) -> None:
    """plan·phase당 step 수 상한. 넘으면 분할을 요청해야 한다."""
    if sum(steps_per_phase) > max_plan or any(n > max_phase for n in steps_per_phase):
        raise PlanTooLarge(f"plan {sum(steps_per_phase)}개 / phase 최대 {max(steps_per_phase, default=0)}개 (상한 {max_plan}/{max_phase})")


def parse_structured(raw: Any, model: Type[BaseModel]) -> BaseModel:
    """모델 출력 한 번을 스키마로 검증한다. 맞지 않으면 ValidationError·ValueError. (재시도 흐름은 `validate_structured`와 `planner/structured.py`의 그래프가 정한다)"""
    return model.model_validate_json(raw) if isinstance(raw, (str, bytes)) else model.model_validate(raw)


def schema_violation(attempts: int, last: Exception | None) -> SchemaViolation:
    return SchemaViolation(f"스키마 검증 실패({attempts}회): {type(last).__name__}")


def validate_structured(call: Callable[[], Any], model: Type[BaseModel], *, max_retries: int = 2) -> BaseModel:
    """모델 출력을 스키마로 검증한다. 실패하면 재시도 상한까지 다시 호출하고, 그래도 안 되면 SchemaViolation(사람에게 넘김)."""
    last: Exception | None = None
    for _ in range(max_retries + 1):
        raw = call()
        try:
            return parse_structured(raw, model)
        except (ValidationError, ValueError) as e:
            last = e
    raise schema_violation(max_retries + 1, last)
