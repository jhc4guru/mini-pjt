"""계획 에이전트(MAIN). 접수 결과 → 읽기 도구로 조사 → phase 윤곽 제출 → phase별 step 제출 → 서버 검증 → `Plan`.

- 모델은 제안만 한다. 도구는 읽기 전용 3개(`ReadToolbox`)와 결과 제출 통로 3개(`submit_outline`, `submit_phase`,
  `request_clarification`)뿐이고, 제출 통로는 아무것도 실행하지 않는다. 그 밖의 도구 이름은 오류로 돌려준다(실행하지 않는다).
- 큰 계획은 phase 윤곽을 먼저 받고 phase를 하나씩 받는다(D-26: 한 번에 만들면 출력 한도에 걸린다). phase를 받을 때마다
  지금까지의 계획 전체를 `build_plan`으로 검증하고, 오류 코드를 모델에 돌려줘 고치게 한다(수정 기회 `MAX_REPAIRS`).
- 모델 출력에 실제 값이 있으면 재시도하지 않고 중단한다(fail-closed). 상한 초과, 모델 사용 불가, 수정 기회 소진도 예외로
  새지 않고 `stopped` 결과로 사람에게 넘긴다. 정상 종료는 `plan`(검증된 Plan)과 `clarify`(되묻기)다.
- 호출은 `LLMClient`를 거치므로 발송 전 누출 검사, MAIN→SUB 폴백(D-19), 토큰 합산이 그대로 적용된다.
- 대화 루프(도구 호출, 수정, 중단, 압축)는 `toolloop.ToolLoop`이 맡고 이 모듈은 계획 단계의 흐름만 가진다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.tools import ReadToolbox, tool_specs
from app.guards.agent_limits import MAX_PHASE_STEPS, AgentBudget, GuardLimitError
from app.guards.audit import AuditLog
from app.guards.boundary import LeakError
from app.guards.command_guard import CommandGuard
from app.guards.masking import Masker
from app.llm import LLMClient, LLMUnavailable
from app.planner.intake import IntakeResult
from app.planner.prompts import DRAFTS_NOTE, SUBMIT_TOOLS, SYSTEM_PROMPT
from app.planner.schema import STEP_ID, Plan, PlanDraft, PlanInvalid, PhaseDraft, StepDraft, build_plan
from app.planner.toolloop import LoopStop, ToolLoop, schema_codes, submit_spec

MAX_REPAIRS = 2          # 한 단계에서 잘못된 제출·도구 호출에 대해 고칠 기회
MAX_NUDGES = 2           # 도구 호출 없이 평문으로 답했을 때 다시 요청하는 횟수
MAX_OUTPUT_TOKENS = 8192  # 계획 출력 한도(D-26: 8K 이상)


# ---------------- 제출 통로의 입력 스키마 ----------------
class _OutlinePhase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=STEP_ID)
    title: str = Field(min_length=1, max_length=100)
    purpose: str = Field(default="", max_length=200)
    preconditions: list[str] = Field(default_factory=list, max_length=8)
    gate: bool = False
    gate_reason: str = Field(default="", max_length=160)
    rollback: str = Field(default="", max_length=300)


class _OutlineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=300)
    phases: list[_OutlinePhase] = Field(min_length=1, max_length=20)


class _PhaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[StepDraft] = Field(min_length=1, max_length=MAX_PHASE_STEPS)


class _Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    param: str = Field(max_length=40)
    question: str = Field(max_length=120)


class _ClarifyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[_Question] = Field(min_length=1, max_length=3)


SUBMIT_SPECS = [
    submit_spec("submit_outline", "계획의 phase 윤곽(요약, phase 목록)을 제출한다. step은 아직 넣지 않는다.", _OutlineInput),
    submit_spec("submit_phase", "서버가 요청한 phase 하나의 step 목록을 제출한다.", _PhaseInput),
    submit_spec("request_clarification", "작업에 꼭 필요한 정보가 요청에 없을 때 사용자에게 묻는다(최대 3개).", _ClarifyInput),
]
assert {s["toolSpec"]["name"] for s in SUBMIT_SPECS} == set(SUBMIT_TOOLS)


@dataclass(frozen=True)
class PlanResult:
    status: str                                   # plan | clarify | stopped
    plan: Plan | None = None
    questions: tuple[dict, ...] = ()
    reasons: tuple[str, ...] = ()                 # stopped일 때 코드
    detail: tuple[str, ...] = ()                  # stopped일 때 부가 정보(도구 이름, 누출 종류 이름. 값 없음)
    roles: tuple[str, ...] = ()                   # 호출마다 요청 역할->실제 역할
    usage: dict = field(default_factory=dict)
    repairs: int = 0
    repair_log: tuple[str, ...] = ()              # 수정 요청에 쓰인 오류 코드(진단용, 값 없음)
    token_log: tuple[tuple[int, int], ...] = ()   # 호출별 (입력, 출력) 토큰


class PlanningAgent:
    def __init__(
        self, llm: LLMClient, toolbox: ReadToolbox, guard: CommandGuard, masker: Masker, budget: AgentBudget,
        *, audit: AuditLog | None = None, max_repairs: int = MAX_REPAIRS,
    ):
        self._llm, self._toolbox, self._guard, self._masker, self._budget = llm, toolbox, guard, masker, budget
        self._audit, self._max_repairs = audit, max_repairs
        self._tools = tool_specs() + SUBMIT_SPECS

    # ---- 공개 ----
    def run(self, intake: IntakeResult, *, iac_managed: bool = False, session_id: str | None = None, drafts: bool = False) -> PlanResult:
        if intake.decision != "proceed" or not intake.masked_text:
            raise ValueError("접수가 proceed인 요청만 계획할 수 있다")
        self._session, self._iac, self._drafts = session_id, iac_managed, drafts
        self._loop = ToolLoop(
            llm=self._llm, toolbox=self._toolbox, masker=self._masker, budget=self._budget, system=SYSTEM_PROMPT, tools=self._tools,
            submit_tools=frozenset(SUBMIT_TOOLS), role="MAIN", max_tokens=MAX_OUTPUT_TOKENS, max_repairs=self._max_repairs,
            max_nudges=MAX_NUDGES, session_id=session_id, truncated_hint="직전 출력이 길이 한도에서 잘렸다. step 수와 설명을 줄여 다시 제출하라.",
        )
        try:
            result = self._run(intake)
        except LoopStop as e:
            result = self._result("stopped", reasons=(e.code,), detail=tuple(e.detail))
        except GuardLimitError as e:
            result = self._result("stopped", reasons=(getattr(e, "code", "limit"),), detail=(e.detail,) if getattr(e, "detail", "") else ())
        except LLMUnavailable as e:
            result = self._result("stopped", reasons=("llm_unavailable",), detail=(str(e)[:100],))      # 원인 종류(쓰로틀링·오류 종류)만. 값은 없다
        except LeakError:
            result = self._result("stopped", reasons=("leak_blocked",))
        self._log(result)
        return result

    # ---- 흐름 ----
    def _run(self, intake: IntakeResult) -> PlanResult:
        loop = self._loop
        loop.user(self._task_text(intake))
        outline: dict = {}

        def on_outline(inp: dict) -> tuple[bool, Any]:
            try:
                o = _OutlineInput.model_validate(inp)
            except ValidationError as e:
                return False, schema_codes(e)
            ids = [p.id for p in o.phases]
            if len(set(ids)) != len(ids):
                return False, ["duplicate_phase_id"]
            outline["value"] = o
            return True, len(o.phases)

        def on_clarify(inp: dict) -> tuple[bool, Any]:
            try:
                return True, [q.model_dump() for q in _ClarifyInput.model_validate(inp).questions]
            except ValidationError as e:
                return False, schema_codes(e)

        # 윤곽이 수락되면 조사 대화(검색 결과 등)는 버리고 다음 요청마다 윤곽과 찾은 문서 명령을 요약으로 준다(입력 토큰 절감)
        name, payload = loop.stage({"submit_outline": on_outline, "request_clarification": on_clarify}, compact=True)
        if name == "request_clarification":
            return self._result("clarify", questions=tuple(payload))

        o: _OutlineInput = outline["value"]
        done: list[PhaseDraft] = []
        for op in o.phases:
            loop.user(self._phase_text(op, done, o))
            holder: dict = {}

            def on_phase(inp: dict, op=op, holder=holder) -> tuple[bool, Any]:
                try:
                    steps = _PhaseInput.model_validate(inp).steps
                except ValidationError as e:
                    return False, schema_codes(e)
                phase = PhaseDraft(id=op.id, title=op.title, purpose=op.purpose, preconditions=op.preconditions, steps=steps,
                                   gate=op.gate, gate_reason=op.gate_reason, rollback=op.rollback)
                try:                                         # 지금까지의 계획 전체를 서버가 검증한다
                    build_plan(PlanDraft(summary=o.summary, phases=[*done, phase]), self._guard, self._masker, iac_managed=self._iac)
                except PlanInvalid as e:
                    if "model_output_unsafe" in e.codes:
                        raise LoopStop("model_output_unsafe", detail=e.detail) from None
                    return False, e.codes[:8]
                holder["phase"] = phase
                return True, len(steps)

            loop.stage({"submit_phase": on_phase}, compact=True)
            done.append(holder["phase"])

        plan = build_plan(PlanDraft(summary=o.summary, phases=done), self._guard, self._masker, iac_managed=self._iac)
        return self._result("plan", plan=plan)

    # ---- 메시지 ----
    def _task_text(self, intake: IntakeResult) -> str:
        interp = "\n".join(f"- {i['alias']} → {i['choice']}" for i in intake.interpretations) or "(없음)"
        return (self._drafts_note() if getattr(self, "_drafts", False) else "") + (
            f"<user_request>\n{intake.masked_text}\n</user_request>\n\n"
            f"정규화 질의: {intake.normalized_query}\n서비스: {', '.join(intake.services) or '(미정)'}\n의도: {intake.intent}\n"
            f"해석:\n{interp}\n\n"
            "필요하면 도구로 조사한 뒤 submit_outline으로 phase 윤곽을 제출하거나, 꼭 필요한 정보가 없으면 request_clarification으로 물어라."
        )

    @staticmethod
    def _drafts_note() -> str:
        return DRAFTS_NOTE + "\n\n"

    def _phase_text(self, op: _OutlinePhase, earlier: list[PhaseDraft], outline: _OutlineInput) -> str:
        plan_txt = "\n".join(f"- {p.id}: {p.title}" + (f" — {p.purpose}" if p.purpose else "") + (" [게이트]" if p.gate else "") for p in outline.phases)
        docs = "\n".join(f"- {cmd} ({kind})" for cmd, kind in list(self._loop.docs_seen.values())[:40]) or "(없음)"
        prior = "\n".join(
            f"[{p.id}] {p.title}\n" + "\n".join(f"- {s.id} ({s.kind}): {(s.command or s.title)[:110]}" for s in p.steps) for p in earlier
        ) or "(없음)"
        return (
            f"iac_managed: {'true' if self._iac else 'false'}\n"
            f"수락된 계획 윤곽: {outline.summary}\n{plan_txt}\n\n"
            f"지금까지 문서에서 찾은 명령(필요하면 search_docs로 더 찾는다):\n{docs}\n\n"
            f"이제 phase `{op.id}`({op.title})의 step을 submit_phase로 제출하라. 목적: {op.purpose or '(미기재)'}. "
            f"step은 최대 {MAX_PHASE_STEPS}개다.\n앞선 phase의 step 요약(id로 depends_on, check_for 참조 가능. 수락된 내용이며 다시 제출하지 않는다):\n{prior}"
        )

    # ---- 결과 ----
    def _result(self, status: str, **kw) -> PlanResult:
        lp = self._loop
        return PlanResult(status, roles=tuple(lp.roles), usage=self._budget.usage(), repairs=lp.repairs, repair_log=tuple(lp.repair_log),
                          token_log=tuple(lp.token_log), **kw)

    def _log(self, result: PlanResult) -> None:
        if self._audit is None:
            return
        if result.status == "stopped":
            tail = (":" + ",".join(result.detail)) if result.detail else ""
            self._audit.record(self._session, "limits", False, (f"agent_stopped:{result.reasons[0]}{tail}",))
        else:
            self._audit.record(self._session, "command", True, (f"agent_{result.status}",))
