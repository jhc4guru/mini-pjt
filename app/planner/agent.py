"""계획 에이전트(MAIN). 접수 결과 → 읽기 도구로 조사 → phase 윤곽 제출 → phase별 step 제출 → 서버 검증 → `Plan`.

- 모델은 제안만 한다. 도구는 읽기 전용 3개(`ReadToolbox`)와 결과 제출 통로 3개(`submit_outline`, `submit_phase`,
  `request_clarification`)뿐이고, 제출 통로는 아무것도 실행하지 않는다. 그 밖의 도구 이름은 오류로 돌려준다(실행하지 않는다).
- 큰 계획은 phase 윤곽을 먼저 받고 phase를 하나씩 받는다(D-26: 한 번에 만들면 출력 한도에 걸린다). phase를 받을 때마다
  지금까지의 계획 전체를 `build_plan`으로 검증하고, 오류 코드를 모델에 돌려줘 고치게 한다(수정 기회 `MAX_REPAIRS`).
- 모델 출력에 실제 값이 있으면 재시도하지 않고 중단한다(fail-closed). 상한 초과, 모델 사용 불가, 수정 기회 소진도 예외로
  새지 않고 `stopped` 결과로 사람에게 넘긴다. 정상 종료는 `plan`(검증된 Plan)과 `clarify`(되묻기)다.
- 호출은 `LLMClient`를 거치므로 발송 전 누출 검사, MAIN→SUB 폴백(D-19), 토큰 합산이 그대로 적용된다.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.tools import ReadToolbox, tool_specs
from app.guards.agent_limits import MAX_PHASE_STEPS, READ_ONLY_TOOLS, AgentBudget, GuardLimitError
from app.guards.audit import AuditLog
from app.guards.boundary import LeakError, leak_kinds
from app.guards.command_guard import CommandGuard
from app.guards.masking import Masker
from app.llm import LLMClient, LLMUnavailable, flatten_text
from app.planner.intake import IntakeResult
from app.planner.prompts import SUBMIT_TOOLS, SYSTEM_PROMPT
from app.planner.schema import STEP_ID, Plan, PlanDraft, PlanInvalid, PhaseDraft, StepDraft, build_plan

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


def _inline_refs(schema: dict) -> dict:
    """Converse 도구 스키마용으로 `$ref`를 인라인으로 풀고 `$defs`를 없앤다."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items() if k not in ("$defs", "title")}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


def _spec(name: str, desc: str, model: type[BaseModel]) -> dict:
    return {"toolSpec": {"name": name, "description": desc, "inputSchema": {"json": _inline_refs(model.model_json_schema())}}}


SUBMIT_SPECS = [
    _spec("submit_outline", "계획의 phase 윤곽(요약, phase 목록)을 제출한다. step은 아직 넣지 않는다.", _OutlineInput),
    _spec("submit_phase", "서버가 요청한 phase 하나의 step 목록을 제출한다.", _PhaseInput),
    _spec("request_clarification", "작업에 꼭 필요한 정보가 요청에 없을 때 사용자에게 묻는다(최대 3개).", _ClarifyInput),
]
assert {s["toolSpec"]["name"] for s in SUBMIT_SPECS} == set(SUBMIT_TOOLS)


@dataclass(frozen=True)
class PlanResult:
    status: str                                   # plan | clarify | stopped
    plan: Plan | None = None
    questions: tuple[dict, ...] = ()
    reasons: tuple[str, ...] = ()                 # stopped일 때 코드
    roles: tuple[str, ...] = ()                   # 호출마다 요청 역할->실제 역할
    usage: dict = field(default_factory=dict)
    repairs: int = 0


class _Stop(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _tool_result(use: dict, payload: dict, ok: bool) -> dict:
    return {"toolResult": {"toolUseId": use["toolUseId"], "status": "success" if ok else "error", "content": [{"json": payload}]}}


def _schema_codes(e: ValidationError) -> list[str]:
    """입력 값은 싣지 않고 위치와 오류 종류만 돌려준다."""
    return [f"schema:{'.'.join(str(p) for p in err['loc'])}:{err['type']}" for err in e.errors()[:6]]


class PlanningAgent:
    def __init__(
        self, llm: LLMClient, toolbox: ReadToolbox, guard: CommandGuard, masker: Masker, budget: AgentBudget,
        *, audit: AuditLog | None = None, max_repairs: int = MAX_REPAIRS,
    ):
        self._llm, self._toolbox, self._guard, self._masker, self._budget = llm, toolbox, guard, masker, budget
        self._audit, self._max_repairs = audit, max_repairs
        self._tools = tool_specs() + SUBMIT_SPECS

    # ---- 공개 ----
    def run(self, intake: IntakeResult, *, iac_managed: bool = False, session_id: str | None = None) -> PlanResult:
        if intake.decision != "proceed" or not intake.masked_text:
            raise ValueError("접수가 proceed인 요청만 계획할 수 있다")
        self._messages: list[dict] = []
        self._pending: list[dict] = []
        self._roles: list[str] = []
        self._repairs = 0
        self._session, self._iac = session_id, iac_managed
        try:
            result = self._run(intake)
        except _Stop as e:
            result = self._stopped(e.code)
        except GuardLimitError as e:
            result = self._stopped(getattr(e, "code", "limit"))
        except LLMUnavailable:
            result = self._stopped("llm_unavailable")
        except LeakError:
            result = self._stopped("leak_blocked")
        self._log(result)
        return result

    # ---- 흐름 ----
    def _run(self, intake: IntakeResult) -> PlanResult:
        self._user(self._task_text(intake))
        outline: dict = {}

        def on_outline(inp: dict) -> tuple[bool, Any]:
            try:
                o = _OutlineInput.model_validate(inp)
            except ValidationError as e:
                return False, _schema_codes(e)
            ids = [p.id for p in o.phases]
            if len(set(ids)) != len(ids):
                return False, ["duplicate_phase_id"]
            outline["value"] = o
            return True, None

        def on_clarify(inp: dict) -> tuple[bool, Any]:
            try:
                return True, [q.model_dump() for q in _ClarifyInput.model_validate(inp).questions]
            except ValidationError as e:
                return False, _schema_codes(e)

        name, payload = self._stage({"submit_outline": on_outline, "request_clarification": on_clarify})
        if name == "request_clarification":
            return PlanResult("clarify", questions=tuple(payload), roles=tuple(self._roles), usage=self._budget.usage(), repairs=self._repairs)

        o: _OutlineInput = outline["value"]
        done: list[PhaseDraft] = []
        for op in o.phases:
            step_ids = {p.id: [s.id for s in p.steps] for p in done}
            self._user(self._phase_text(op, step_ids))
            holder: dict = {}

            def on_phase(inp: dict, op=op, holder=holder) -> tuple[bool, Any]:
                try:
                    steps = _PhaseInput.model_validate(inp).steps
                except ValidationError as e:
                    return False, _schema_codes(e)
                phase = PhaseDraft(id=op.id, title=op.title, purpose=op.purpose, preconditions=op.preconditions, steps=steps,
                                   gate=op.gate, gate_reason=op.gate_reason, rollback=op.rollback)
                try:                                         # 지금까지의 계획 전체를 서버가 검증한다
                    build_plan(PlanDraft(summary=o.summary, phases=[*done, phase]), self._guard, self._masker, iac_managed=self._iac)
                except PlanInvalid as e:
                    if "model_output_unsafe" in e.codes:
                        raise _Stop("model_output_unsafe") from None
                    return False, e.codes[:8]
                holder["phase"] = phase
                return True, None

            self._stage({"submit_phase": on_phase})
            done.append(holder["phase"])

        plan = build_plan(PlanDraft(summary=o.summary, phases=done), self._guard, self._masker, iac_managed=self._iac)
        return PlanResult("plan", plan=plan, roles=tuple(self._roles), usage=self._budget.usage(), repairs=self._repairs)

    def _stage(self, handlers: dict[str, Callable[[dict], tuple[bool, Any]]]) -> tuple[str, Any]:
        """한 단계: 제출 통로가 허용될 때까지 모델과 도구 호출을 주고받는다. 성공하면 (도구 이름, 값)."""
        repairs = nudges = 0
        while True:
            self._budget.before_step()
            res = self._llm.chat("MAIN", self._messages, system=SYSTEM_PROMPT, tools=self._tools,
                                 max_tokens=MAX_OUTPUT_TOKENS, session_id=self._session)
            self._roles.append(f"{res.requested_role}->{res.actual_role}")
            if leak_kinds(flatten_text(None, [{"content": res.content}]), self._masker):
                raise _Stop("model_output_unsafe")              # 누출이 든 출력은 대화에 남기지 않고 멈춘다
            if res.stop_reason == "max_tokens":                  # 잘린 출력은 대화에 넣지 않고 줄여서 다시 요청한다
                repairs += 1
                self._repairs += 1
                if repairs > self._max_repairs:
                    raise _Stop("output_truncated")
                self._append_text_to_last_user("직전 출력이 길이 한도에서 잘렸다. step 수와 설명을 줄여 다시 제출하라.")
                continue
            self._messages.append({"role": "assistant", "content": res.content})
            uses = res.tool_uses
            if not uses:
                nudges += 1
                if nudges > MAX_NUDGES:
                    raise _Stop("no_submission")
                self._user("평문 답변은 쓰지 않는다. 필요한 도구를 호출하거나 제출 도구로 결과를 제출하라.")
                continue
            results: list[dict] = []
            accepted: tuple[str, Any] | None = None
            for use in uses:
                name, inp = use["name"], use.get("input") or {}
                if name in READ_ONLY_TOOLS:
                    out = self._toolbox.call(name, inp)           # 상한 초과는 예외로 전파된다
                    results.append(_tool_result(use, out, bool(out.get("ok"))))
                elif name in handlers and accepted is None:
                    ok, value = handlers[name](inp)
                    if ok:
                        accepted = (name, value)
                        results.append(_tool_result(use, {"accepted": True}, True))
                    else:
                        repairs += 1
                        self._repairs += 1
                        results.append(_tool_result(use, {"error": value}, False))
                elif name in SUBMIT_TOOLS:
                    repairs += 1
                    self._repairs += 1
                    results.append(_tool_result(use, {"error": ["wrong_stage" if name not in handlers else "duplicate_submission"]}, False))
                else:
                    repairs += 1
                    self._repairs += 1
                    results.append(_tool_result(use, {"error": ["unknown_tool"]}, False))
            self._pending = results
            if accepted is not None:
                return accepted                                   # 결과는 다음 사용자 메시지 앞에 붙는다
            if repairs > self._max_repairs:
                raise _Stop("repair_exhausted")
            self._user(None)

    # ---- 메시지 ----
    def _user(self, text: str | None) -> None:
        content = [*self._pending] + ([{"text": text}] if text else [])
        self._pending = []
        if content:
            self._messages.append({"role": "user", "content": content})

    def _append_text_to_last_user(self, text: str) -> None:
        self._messages[-1]["content"].append({"text": text})

    @staticmethod
    def _task_text(intake: IntakeResult) -> str:
        interp = "\n".join(f"- {i['alias']} → {i['choice']}" for i in intake.interpretations) or "(없음)"
        return (
            f"<user_request>\n{intake.masked_text}\n</user_request>\n\n"
            f"정규화 질의: {intake.normalized_query}\n서비스: {', '.join(intake.services) or '(미정)'}\n의도: {intake.intent}\n"
            f"해석:\n{interp}\n\n"
            "필요하면 도구로 조사한 뒤 submit_outline으로 phase 윤곽을 제출하거나, 꼭 필요한 정보가 없으면 request_clarification으로 물어라."
        )

    def _phase_text(self, op: _OutlinePhase, earlier: dict[str, list[str]]) -> str:
        prior = "\n".join(f"- {pid}: {', '.join(ids)}" for pid, ids in earlier.items()) or "(없음)"
        return (
            f"iac_managed: {'true' if self._iac else 'false'}\n"
            f"이제 phase `{op.id}`({op.title})의 step을 submit_phase로 제출하라. 목적: {op.purpose or '(미기재)'}. "
            f"step은 최대 {MAX_PHASE_STEPS}개다.\n앞선 phase의 step id(depends_on, check_for로 참조 가능):\n{prior}"
        )

    # ---- 결과 ----
    def _stopped(self, code: str) -> PlanResult:
        return PlanResult("stopped", reasons=(code,), roles=tuple(self._roles), usage=self._budget.usage(), repairs=self._repairs)

    def _log(self, result: PlanResult) -> None:
        if self._audit is None:
            return
        if result.status == "stopped":
            self._audit.record(self._session, "limits", False, (f"agent_stopped:{result.reasons[0]}",))
        else:
            self._audit.record(self._session, "command", True, (f"agent_{result.status}",))
