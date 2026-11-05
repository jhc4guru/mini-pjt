"""요청 한 건의 계획 생성 흐름: 접수(입력 가드·마스킹·범위·모호 표현) → 계획 에이전트 → 리뷰어 → 근거 조각.

각 단계는 자기 안에서 가드·검증·중단 처리를 한다. 이 모듈은 단계를 이어 붙이고 결과를 네 가지 상태로 모은다.
- `reject`: 입력 가드가 막았거나 범위 밖이다(사용자 안내문 포함, 값은 되풀이하지 않는다).
- `clarify`: 되물을 것이 있다(접수 단계의 모호 표현·필수 값 누락, 또는 계획 단계의 되묻기).
- `plan`: 검증된 `Plan`, 서버 규칙 체크리스트·위험 등급과 모델 요약(있으면), step별 근거 조각.
- `stopped`: 모델·한도·검증 때문에 계속할 수 없어 사람에게 넘긴다.
승인 엔진(T6)이 이 결과를 받아 plan 검토 화면으로 넘긴다. 이 모듈은 아무것도 실행하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.guards.input_guard import user_message
from app.planner.agent import PlanningAgent, PlanResult
from app.planner.intake import Intake, IntakeResult
from app.planner.review import ReviewResult, Reviewer, step_evidence
from app.rag.chunks import Chunk

_MESSAGES = {
    "out_of_scope": "지원하지 않는 요청입니다. 이 도구는 AWS(EC2/보안 그룹, EKS, S3, IAM)와 kubectl 작업 계획만 다룹니다.",
    "model_output_unsafe": "모델 출력에서 민감값이 감지되어 계획을 만들지 않았습니다.",
}
# 계획 생성이 중단됐을 때의 안내(원인 코드별). 값은 들어 있지 않다.
_STOP_MESSAGES = {
    "repeated_call": "에이전트가 같은 조회를 반복해서 멈췄습니다(반복 호출 상한). ‘다시 시도’를 누르거나 대상(이름, 범위)을 더 구체적으로 알려 주세요.",
    "tool_calls_exceeded": "조사에 필요한 도구 호출이 한도를 넘어 멈췄습니다. 요청 범위를 줄이거나 나눠서 요청해 주세요.",
    "steps_exceeded": "추론 단계가 한도를 넘어 멈췄습니다. 요청 범위를 줄이거나 나눠서 요청해 주세요.",
    "tokens_exceeded": "모델 사용량 한도를 넘어 멈췄습니다. 요청 범위를 줄이거나 나눠서 요청해 주세요.",
    "time_exceeded": "계획 생성 시간이 한도를 넘어 멈췄습니다. ‘다시 시도’를 누르거나 요청을 나눠 주세요.",
    "llm_unavailable": "모델을 지금 사용할 수 없습니다(요청 제한 등). 잠시 뒤 ‘다시 시도’를 눌러 주세요.",
    "output_truncated": "모델 응답이 길이 한도에서 잘려 멈췄습니다. 요청 범위를 줄여 주세요.",
    "no_submission": "에이전트가 계획을 제출하지 않고 멈췄습니다. ‘다시 시도’를 눌러 주세요.",
    "repair_exhausted": "서버 검증을 통과하는 계획을 만들지 못했습니다. 내용을 더 구체적으로 보태 주세요.",
    "plan_too_large": "계획이 너무 커서 멈췄습니다. 작업을 나눠서 요청해 주세요.",
    "leak_blocked": "민감값이 감지되어 모델 호출을 막았습니다.",
}
_LEAK_KINDS = {"resource_id": "리소스 ID", "ip": "IP/CIDR", "account_id": "계정 ID", "arn": "ARN", "access_key": "액세스 키", "email": "이메일", "name": "리소스 이름"}


def stop_message(code: str, detail: tuple[str, ...] = ()) -> str:
    """중단 원인 코드를 사용자 안내로 바꾼다. 누출 종류와 반복된 도구 이름은 이름만 덧붙인다(값 없음)."""
    if code == "model_output_unsafe":
        kinds = ", ".join(_LEAK_KINDS.get(k, k) for k in detail) if detail else ""
        return "모델 출력에서 민감값" + (f"({kinds})" if kinds else "") + "이 감지되어 계획을 만들지 않았습니다. ‘다시 시도’를 누르거나 대상을 더 구체적으로 알려 주세요."
    msg = _STOP_MESSAGES.get(code, "계획 생성을 계속할 수 없어 중단했습니다. ‘다시 시도’를 누르거나 요청을 나눠 주세요.")
    return msg + (f" (반복된 도구: {detail[0]})" if code == "repeated_call" and detail else "")


@dataclass(frozen=True)
class PipelineResult:
    status: str                                       # reject | clarify | plan | stopped
    message: str = ""                                 # 사용자에게 보일 안내(값 없음)
    intake: IntakeResult | None = None
    plan_result: PlanResult | None = None
    review: ReviewResult | None = None
    evidence: dict = field(default_factory=dict)      # step id → 근거(문서 텍스트 또는 "AI 추정")
    questions: tuple[dict, ...] = ()
    reasons: tuple[str, ...] = ()
    detail: tuple[str, ...] = ()                      # 중단 부가 정보(도구 이름, 누출 종류 이름. 값 없음)


class PlanPipeline:
    def __init__(self, intake: Intake, agent: PlanningAgent, reviewer: Reviewer, chunks: dict[str, Chunk]):
        self._intake, self._agent, self._reviewer, self._chunks = intake, agent, reviewer, chunks

    def run(self, text: str, *, iac_managed: bool = False, session_id: str | None = None) -> PipelineResult:
        intake = self._intake.process(text, session_id=session_id)
        if intake.decision == "reject":
            msg = _MESSAGES.get(intake.reasons[0], "") if intake.reasons else ""
            return PipelineResult("reject", message=msg or user_message(intake.reasons), intake=intake, reasons=intake.reasons)
        if intake.decision == "clarify":
            return PipelineResult("clarify", message="요청을 정확히 이해하려면 아래 내용을 알려 주세요.", intake=intake, questions=intake.questions)

        result = self._agent.run(intake, iac_managed=iac_managed, session_id=session_id)
        if result.status == "clarify":
            return PipelineResult("clarify", message="계획을 만들려면 아래 내용을 알려 주세요.", intake=intake, plan_result=result, questions=result.questions)
        if result.status != "plan" or result.plan is None:
            code = result.reasons[0] if result.reasons else "stopped"
            return PipelineResult("stopped", message=stop_message(code, result.detail), intake=intake, plan_result=result, reasons=result.reasons, detail=result.detail)
        review = self._reviewer.review(result.plan, session_id=session_id)
        evidence = {s.id: step_evidence(s, self._chunks) for s in result.plan.all_steps()}
        return PipelineResult("plan", intake=intake, plan_result=result, review=review, evidence=evidence)
