"""서버 실행의 단일 관문. 실행기(executor)는 명령을 실행하기 전에 반드시 이 함수를 통과해야 한다.

- 가드가 허용하지 않은 명령, manual 명령은 어떤 승인으로도 실행하지 않는다.
- 변경(mutate) 명령은 요청자와 다른 사람의 승인이 있어야 한다(4-eyes).
- 가드가 요구한 추가 조치(verdict.requires)는 승인 단계에서 모두 확인(confirmations)되어야 한다.
"""
from __future__ import annotations

from app.guards.agent_limits import ToolNotAllowed
from app.guards.command_guard import CommandVerdict


class ExecutionDenied(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def assert_server_executable(
    verdict: CommandVerdict,
    *,
    approved_by_other: bool = False,
    confirmations: frozenset[str] | set[str] = frozenset(),
) -> None:
    """실행해도 되면 조용히 반환하고, 아니면 ExecutionDenied를 낸다."""
    if not verdict.allow:
        raise ExecutionDenied("blocked_by_guard")
    if verdict.kind == "manual" or not verdict.executable:
        raise ExecutionDenied("manual_command_never_executed_by_server")
    if verdict.kind not in ("read", "mutate"):
        raise ExecutionDenied("unknown_kind")
    missing = [r for r in verdict.requires if r not in confirmations]
    if missing:
        raise ExecutionDenied("missing_confirmation:" + ",".join(missing))
    if verdict.kind == "mutate" and not approved_by_other:
        raise ExecutionDenied("four_eyes_approval_required")


def assert_read_only_tool_call(verdict: CommandVerdict) -> None:
    """에이전트의 읽기 도구(run_read_command)가 변경 명령을 실행하지 못하게 한다."""
    if not (verdict.allow and verdict.executable and verdict.kind == "read" and verdict.grounded and not verdict.requires):
        raise ToolNotAllowed("읽기 도구는 문서에 근거한 읽기 명령만 실행한다")
