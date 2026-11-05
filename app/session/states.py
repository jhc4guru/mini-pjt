"""세션·step 상태와 허용되는 전이. 전이는 `store`가 이 표로만 허용하고, 표에 없는 전이는 `InvalidTransition`이다."""
from __future__ import annotations

SESSION_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"plan_review", "aborted"}),
    "plan_review": frozenset({"plan_approved", "aborted"}),
    "plan_approved": frozenset({"applying", "plan_review", "aborted"}),
    "applying": frozenset({"paused", "completed", "aborted"}),
    "paused": frozenset({"applying", "plan_review", "aborted"}),      # paused → plan_review: 부분 재계획(새 버전, 재승인)
    "completed": frozenset(),
    "aborted": frozenset(),
}

STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"approved", "aborted"}),
    "approved": frozenset({"running", "awaiting_report", "pending", "aborted"}),
    "running": frozenset({"done", "failed", "aborted"}),
    "awaiting_report": frozenset({"awaiting_confirm", "failed", "aborted"}),      # manual·iac: 요청자의 실행 완료 보고를 기다린다
    "awaiting_confirm": frozenset({"done", "awaiting_report", "aborted"}),         # 다른 사람 확인(거절하면 다시 보고 대기)
    "failed": frozenset({"pending", "skipped", "aborted"}),                        # 재시도는 pending으로 돌아가 재승인을 받는다
    "done": frozenset(),
    "skipped": frozenset(),
    "aborted": frozenset(),
}
TERMINAL_STEP = frozenset({"done", "skipped", "aborted"})
SATISFIED_STEP = frozenset({"done", "skipped"})          # 의존하는 step이 진행해도 되는 상태

APPROVAL_KINDS = frozenset({"plan", "step", "gate", "confirm", "report", "verify"})


class InvalidTransition(ValueError):
    def __init__(self, kind: str, src: str, dst: str):
        super().__init__(f"{kind}:{src}->{dst}")
        self.code = f"invalid_transition:{kind}:{src}->{dst}"


def check_session(src: str, dst: str) -> None:
    if dst not in SESSION_TRANSITIONS.get(src, frozenset()):
        raise InvalidTransition("session", src, dst)


def check_step(src: str, dst: str) -> None:
    if dst not in STEP_TRANSITIONS.get(src, frozenset()):
        raise InvalidTransition("step", src, dst)
