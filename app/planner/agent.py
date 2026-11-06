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

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agents.tools import ReadToolbox
from app.guards.agent_limits import MAX_PHASE_STEPS, READ_ONLY_TOOLS, AgentBudget, GuardLimitError
from app.guards.audit import AuditLog
from app.guards.boundary import LeakError, leak_kinds
from app.guards.command_guard import CommandGuard
from app.guards.masking import Masker
from app.guards.untrusted import sanitize_untrusted
from app.llm import LLMClient, LLMUnavailable
from app.planner.checkpoint import PlanCheckpoint, request_key
from app.planner.intake import IntakeResult
from app.planner.scenarios import Scenario, ScenarioMatch, match_scenario, missing_expectations
from app.planner.prompts import (ALL_SUBMIT_TOOLS, BATCH_INV_PROMPT, BATCH_PHASE_PROMPT, BATCH_SYSTEM_PROMPT, DRAFTS_NOTE, INV_MAX_COMMANDS, INV_MAX_INVENTORY, INV_MAX_QUERIES, INV_MAX_ROUNDS,
                                 SUBMIT_TOOLS, SYSTEM_PROMPT)
from app.planner.schema import STEP_ID, Plan, PlanDraft, PlanInvalid, PhaseDraft, StepDraft, build_plan
from app.planner.toolloop import LoopStop, ToolLoop, schema_codes, submit_spec, tool_result

MAX_REPAIRS = 2          # 한 단계에서 잘못된 제출·도구 호출에 대해 고칠 기회
MAX_NUDGES = 2           # 도구 호출 없이 평문으로 답했을 때 다시 요청하는 횟수
MAX_OUTPUT_TOKENS = 8192  # 계획 출력 한도(D-26: 8K 이상)
PHASE_DOC_HITS = 6          # phase마다 서버가 미리 검색해 싣는 문서 수(첫 질의, D-71)
READS_PER_PHASE = 12     # phase 요청 하나에 붙이는 읽기 결과 요약의 최대 개수(나머지는 다음 phase로)
READ_DIGEST_CHARS = 450  # 읽기 결과 하나의 요약 길이(D-57)


def _digest(framed: str, limit: int = READ_DIGEST_CHARS) -> str:
    """`sanitize_untrusted`가 감싼 읽기 결과에서 본문을 꺼내 한 줄로 줄인다(JSON이면 공백 없이 압축). 틀은 호출하는 쪽이 요약 전체에 다시 씌운다."""
    body = framed
    if framed.startswith("<untrusted_data"):
        parts = framed.split("\n", 2)
        body = parts[2] if len(parts) == 3 else ""
        body = body.rsplit("\n</untrusted_data>", 1)[0]
    try:
        body = json.dumps(json.loads(body), ensure_ascii=False, separators=(",", ":"))
    except ValueError:
        body = re.sub(r"\s+", " ", body).strip()
    return body if len(body) <= limit else body[:limit] + "…(이하 생략)"


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


class _InvestigationInput(BaseModel):
    """일괄 조사(D-69): 서버가 항목마다 검사를 통과한 읽기 전용 조회만 실행한다. 이 입력은 아무것도 실행하지 않는다."""
    model_config = ConfigDict(extra="forbid")
    doc_queries: list[str] = Field(default_factory=list, max_length=INV_MAX_QUERIES)
    commands: list[str] = Field(default_factory=list, max_length=INV_MAX_COMMANDS)
    inventory: list[str] = Field(default_factory=list, max_length=INV_MAX_INVENTORY)

    @model_validator(mode="after")
    def _not_empty(self):
        if not (self.doc_queries or self.commands or self.inventory):
            raise ValueError("empty_investigation")
        return self


class _Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    param: str = Field(max_length=40)
    question: str = Field(max_length=120)


class _ClarifyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[_Question] = Field(min_length=1, max_length=3)


SUBMIT_SPECS = [
    submit_spec("submit_investigation", "필요한 읽기 조회(문서 검색 질의, 읽기 명령, 인벤토리 카테고리)를 한꺼번에 제출한다. 서버가 검사를 통과한 것만 모두 실행해 결과를 돌려준다.", _InvestigationInput),
    submit_spec("submit_outline", "계획의 phase 윤곽(요약, phase 목록)을 제출한다. step은 아직 넣지 않는다.", _OutlineInput),
    submit_spec("submit_phase", "서버가 요청한 phase 하나의 step 목록을 제출한다.", _PhaseInput),
    submit_spec("request_clarification", "작업에 꼭 필요한 정보가 요청에 없을 때 사용자에게 묻는다(최대 3개).", _ClarifyInput),
]
assert {s.name for s in SUBMIT_SPECS} == set(ALL_SUBMIT_TOOLS)
_LOOP_NAMES = READ_ONLY_TOOLS | frozenset(SUBMIT_TOOLS)                                            # 기존(loop) 방식: 모든 단계에서 읽기 도구와 3개 제출 통로
_INV_NAMES = frozenset({"submit_investigation", "submit_outline", "request_clarification"})      # batch 방식 조사·윤곽 단계: 읽기 도구는 보이지 않는다(일괄 제출을 강제)
_PHASE_NAMES = READ_ONLY_TOOLS | frozenset({"submit_phase"})                                      # batch 방식 phase 단계: 보충 조회만


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
    token_log: tuple[tuple[int, int], ...] = ()   # 호출별 (입력, 출력) 토큰. 입력은 캐시 읽기·쓰기를 포함한 전체
    cache_log: tuple[tuple[int, int], ...] = ()   # 호출별 (캐시 읽기, 캐시 쓰기) 토큰(프롬프트 캐싱 측정용)
    checkpoint: PlanCheckpoint | None = None      # stopped일 때 이어서 할 수 있는 진행 상태(D-68)
    resumed_phases: int = 0                       # 체크포인트에서 이어받은 phase 수(0이면 처음부터)
    stage_log: tuple[str, ...] = ()               # 모델 호출마다 단계 이름(계측용)
    tool_log: tuple[tuple[str, str], ...] = ()    # 읽기 도구 호출마다 (단계 이름, 도구 이름)(계측용)


class PlanningAgent:
    def __init__(
        self, llm: LLMClient, toolbox: ReadToolbox, guard: CommandGuard, masker: Masker, budget: AgentBudget,
        *, audit: AuditLog | None = None, max_repairs: int = MAX_REPAIRS, mode: str = "loop", stage_prompts: bool = False, phase_docs: bool = True,
        scenarios: list[Scenario] | None = None,
    ):
        if mode not in ("loop", "batch"):
            raise ValueError("mode는 loop 또는 batch")
        self._mode = mode
        self._scenarios = list(scenarios or [])          # 시나리오 묶음(D-72). batch 방식에서 요청이 맞으면 조사·윤곽을 시나리오가 대신한다
        self._phase_docs_on = phase_docs                 # batch에서 phase마다 서버가 문서를 미리 검색해 요청에 싣는다(D-71). 측정 비교용 선택이며 가드와 무관하다
        self._stage_prompts = stage_prompts              # batch에서 단계별 시스템 프롬프트(D-70)를 쓸지. 측정에서 오히려 나빠 기본은 끈다(D-70 번복). 비교 실험용이며 가드와 무관하다
        self._llm, self._toolbox, self._guard, self._masker, self._budget = llm, toolbox, guard, masker, budget
        self._audit, self._max_repairs = audit, max_repairs
        self._tools = toolbox.as_tools() + SUBMIT_SPECS

    # ---- 공개 ----
    def run(self, intake: IntakeResult, *, iac_managed: bool = False, session_id: str | None = None, drafts: bool = False,
            resume: PlanCheckpoint | None = None, on_checkpoint: Callable[[PlanCheckpoint], None] | None = None) -> PlanResult:
        if intake.decision != "proceed" or not intake.masked_text:
            raise ValueError("접수가 proceed인 요청만 계획할 수 있다")
        self._session, self._iac, self._drafts = session_id, iac_managed, drafts
        self._key = request_key(intake.masked_text, iac_managed)
        self._resume, self._on_cp = resume, on_checkpoint
        self._outline_dump: dict | None = None
        self._done_dumps: list[dict] = []
        self._head_sent = False
        self._resumed = 0
        self._queries_done: set[str] = set()
        self._pre_cache: dict[str, list[dict]] = {}       # phase 사전 검색 결과(질의 → [{id, doc}]). 같은 질의를 다시 하지 않는다(반복 호출 상한·예산)
        self._sm: ScenarioMatch | None = (match_scenario(self._scenarios, intake.masked_text, intake.services)
                                           if self._mode == "batch" and self._scenarios else None)
        self._reads_shown: set[str] = set()
        self._docs_shown: set[str] = set()
        self._loop = ToolLoop(
            llm=self._llm, toolbox=self._toolbox, masker=self._masker, budget=self._budget,
            system=BATCH_SYSTEM_PROMPT if self._mode == "batch" else SYSTEM_PROMPT, tools=self._tools,
            submit_tools=frozenset(ALL_SUBMIT_TOOLS), role="MAIN", max_tokens=MAX_OUTPUT_TOKENS, max_repairs=self._max_repairs,
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
        outline: dict = {}
        restored = self._restore(intake)                  # (윤곽, 완료한 phase) 또는 None. 검증에 실패하면 처음부터 한다
        if restored is None and self._sm is not None:
            restored = self._scenario_start(intake)       # 시나리오: 서버가 조사를 일괄 실행하고 윤곽을 정한다(모델 호출 없음)
        elif restored is None:
            loop.user(self._task_text(intake))
        elif restored[0] is None:
            loop.user(self._task_text(intake) + self._investigated_text())      # 조사 단계에서 멈췄던 것: 찾은 문서·읽기 결과를 요약으로 준다
        else:
            o0 = restored[0]
            loop.messages = [HumanMessage(content=self._task_text(intake)),
                             AIMessage(content="", tool_calls=[{"name": "submit_outline", "args": {"accepted_steps": len(o0.phases)}, "id": "resumed-outline", "type": "tool_call"}])]
            loop.pending = [tool_result({"id": "resumed-outline"}, {"accepted": True}, True)]
        inv_start = len(loop.messages)                    # batch 방식은 조사 대화를 윤곽이 수락될 때 이 위치부터 압축한다

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
        if restored is not None and restored[0] is not None:
            o, done = restored
            self._resumed = len(done)
        else:
            if self._mode == "batch":
                name, payload = self._investigate_and_outline(on_outline, on_clarify, inv_start)
            else:
                name, payload = loop.stage({"submit_outline": on_outline, "request_clarification": on_clarify}, compact=True, tool_names=_LOOP_NAMES)
            if name == "request_clarification":
                return self._result("clarify", questions=tuple(payload))
            o = outline["value"]
            done = []
            self._outline_dump = o.model_dump()
            self._checkpoint()
        for op in o.phases[len(done):]:
            loop.label = "phase"
            if self._mode == "batch":
                self._budget.begin_stage(self._budget.limits.phase_calls)            # phase마다 보충 조회 예산을 따로 준다(D-69)
            text = self._phase_text(op, done, o)
            docs_txt = self._phase_docs(op) if self._mode == "batch" and self._phase_docs_on else ""
            loop.user(text + docs_txt)
            msg_idx = len(loop.messages) - 1                      # 이 phase 요청 메시지(수락되면 미리 검색한 문서 본문은 지운다)
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
                sp = self._sm.phase(op.id) if self._sm is not None else None
                if sp is not None and (miss := missing_expectations(sp, steps)):      # 시나리오의 필수 항목이 모자라면 고치게 한다(D-72)
                    return False, miss[:8]
                holder["phase"] = phase
                return True, len(steps)

            handlers = {"submit_phase": on_phase}
            names = _PHASE_NAMES if self._mode == "batch" else _LOOP_NAMES
            if self._sm is not None:                                  # 시나리오 흐름은 윤곽 단계가 없으므로 phase 단계에서도 되물을 수 있다(대상·값이 불명확할 때, D-72)
                handlers["request_clarification"] = on_clarify
                names = names | {"request_clarification"}
            got, payload2 = loop.stage(handlers, compact=True, tool_names=names,
                                       system=BATCH_PHASE_PROMPT if self._mode == "batch" and self._stage_prompts else None)
            if got == "request_clarification":
                return self._result("clarify", questions=tuple(payload2))
            if docs_txt:                                                # 문서 본문은 이 phase에만 필요하다. 뒤 phase의 대화에 계속 실려 가지 않게 지운다(명령 이름은 `docs_seen`에 남는다)
                loop.messages[msg_idx] = HumanMessage(content=text + "\n(이 phase용으로 미리 검색한 문서 본문은 수락 뒤 생략했다. 명령 이름은 위 목록에 있다.)")
            done.append(holder["phase"])
            self._done_dumps.append(holder["phase"].model_dump())
            self._checkpoint()

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

    def _new_docs(self) -> str:
        """지금까지 찾은 문서 명령 중 아직 알려 주지 않은 것(최대 40개). phase 요청은 대화에 남으므로 같은 목록을 phase마다 다시 싣지 않는다(D-58)."""
        shown = getattr(self, "_docs_shown", None)
        if shown is None:
            shown = self._docs_shown = set()
        lines: list[str] = []
        for doc_id, (cmd, kind) in list(self._loop.docs_seen.items()):
            if doc_id in shown:
                continue
            if len(lines) >= 40:
                break
            shown.add(doc_id)
            lines.append(f"- {cmd} ({kind})")
        return "\n".join(lines)

    def _new_reads(self) -> str:
        """압축으로 대화에서 사라진 읽기 결과 중 아직 알려 주지 않은 것의 요약(데이터 틀 안). 모델이 같은 조회를 되풀이해 도구 호출 예산을 쓰지 않게 한다.

        phase 요청은 대화에 남으므로 이미 준 결과는 다시 싣지 않는다. 한 번에 `READS_PER_PHASE`개·`READ_DIGEST_CHARS`자까지만 주고 나머지는 다음 phase로 넘긴다."""
        shown = getattr(self, "_reads_shown", None)
        if shown is None:
            shown = self._reads_shown = set()
        lines: list[str] = []
        for cmd, framed in list(self._loop.reads_seen.items()):
            if cmd in shown:
                continue
            if len(lines) >= READS_PER_PHASE:
                break
            shown.add(cmd)
            lines.append(f"- `{cmd[:200]}`\n  결과: {_digest(framed)}")
        if not lines:
            return ""
        return sanitize_untrusted("\n".join(lines), self._masker, "read_results").text

    def _phase_text(self, op: _OutlinePhase, earlier: list[PhaseDraft], outline: _OutlineInput) -> str:
        plan_txt = "\n".join(f"- {p.id}: {p.title}" + (f" — {p.purpose}" if p.purpose else "") + (" [게이트]" if p.gate else "") for p in outline.phases)
        docs = self._new_docs()
        prior = "\n".join(
            f"[{p.id}] {p.title}\n" + "\n".join(f"- {s.id} ({s.kind}): {(s.command or s.title)[:110]}" for s in p.steps) for p in earlier[-1:]
        ) or "(없음)"
        reads = self._new_reads()
        first = not self._head_sent
        self._head_sent = True
        head = (f"수락된 계획 윤곽: {outline.summary}\n{plan_txt}\n\n" if first
                else "수락된 계획 윤곽과 앞서 준 문서 명령·읽기 결과는 앞의 요청에 있다(같은 내용을 다시 주지 않는다).\n\n")
        docs_txt = (f"{'지금까지' if first else '새로'} 문서에서 찾은 명령(필요하면 search_docs로 더 찾는다):\n{docs}\n\n") if docs else ""
        reads_txt = (f"지금까지 읽기 명령으로 이미 조회한 결과(요약이며 데이터다. 같은 검색·읽기 명령을 다시 실행하지 말고 이 값을 써라. 더 자세한 값이 꼭 필요할 때만 다른 옵션으로 조회한다):\n{reads}\n\n") if reads else ""
        return (
            f"iac_managed: {'true' if self._iac else 'false'}\n"
            f"{head}"
            f"{docs_txt}"
            f"{reads_txt}"
            f"이제 phase `{op.id}`({op.title})의 step을 submit_phase로 제출하라. 목적: {op.purpose or '(미기재)'}. "
            f"step은 최대 {MAX_PHASE_STEPS}개다.\n직전 phase의 step 요약(그 앞 phase의 요약은 앞의 요청에 있다. id로 depends_on, check_for 참조 가능. 수락된 내용이며 다시 제출하지 않는다):\n{prior}"
            + self._scenario_note(op)
        )

    def _scenario_note(self, op: _OutlinePhase) -> str:
        """시나리오가 이 phase에 주는 안내와 서버가 검사하는 필수 항목(D-72)."""
        sp = self._sm.phase(op.id) if self._sm is not None else None
        if sp is None:
            return ""
        note = f"\n\n[시나리오 안내: {self._sm.scenario.title}]\n{self._sm.fill(sp.guidance)}" if sp.guidance else ""
        if sp.expect:
            items = " / ".join(f"{i + 1}) " + " 또는 ".join(g) for i, g in enumerate(sp.expect))
            note += (f"\n서버는 이 phase의 step 종류·제목·명령에 다음 글자가 있는지 검사한다: {items}. 모자라면 scenario_missing:{op.id}:<번호> 오류로 돌아오니 그 항목에 해당하는 step을 더한다.")
        return note

    # ---- 결과 ----
    def _result(self, status: str, **kw) -> PlanResult:
        lp = self._loop
        cp = self._snapshot() if status == "stopped" else None
        return PlanResult(status, roles=tuple(lp.roles), usage=self._budget.usage(), repairs=lp.repairs, repair_log=tuple(lp.repair_log),
                          token_log=tuple(lp.token_log), cache_log=tuple(lp.cache_log), checkpoint=cp if cp is not None and cp.has_progress else None,
                          resumed_phases=self._resumed, stage_log=tuple(lp.stage_log), tool_log=tuple(lp.tool_log), **kw)

    # ---- phase별 문서 사전 검색(D-71) ----
    def _phase_docs(self, op: _OutlinePhase) -> str:
        """phase 제목·목적으로 서버가 문서를 미리 검색해 본문까지 요청에 싣는다. 모델이 phase를 쓰다가 `search_docs`를 부르는 모델 왕복을 줄이려는 것이다.
        기존 `ReadToolbox.call`을 거치므로 마스킹 확인·검색 방식·호출 예산이 그대로 적용된다. 검색이 실패하면(임베딩 오류 등) 아무것도 싣지 않고 모델이 직접 찾게 둔다.
        한도·누출 예외는 그대로 올린다."""
        shown = self._docs_shown
        sp = self._sm.phase(op.id) if self._sm is not None else None
        queries = ([q[:300] for q in sp.docs] if sp is not None and sp.docs
                   else [q[:300] for q in dict.fromkeys([f"{op.title}: {op.purpose}".strip(": "), op.purpose.strip() or op.title.strip()]) if q])
        blocks: list[str] = []
        seen: set[str] = set()                                    # 이 phase 요청 안에서만 중복을 뺀다(앞 phase의 문서 본문은 수락 뒤 지워지므로 뒤 phase에 다시 싣는다)
        try:
            for i, q in enumerate(queries):
                hits = self._pre_cache.get(q)
                if hits is None:                                   # 같은 질의는 한 번만 실행한다(여러 phase가 같은 문서를 쓰는 시나리오에서 반복 호출 상한에 걸리지 않게)
                    out = self._toolbox.call("search_docs", {"query": q, "k": 2 if sp is not None and sp.docs else (PHASE_DOC_HITS if i == 0 else 3)})
                    self._loop.tool_log.append(("phase_pre", "search_docs"))
                    if not out.get("ok"):
                        continue
                    self._loop.record_read("search_docs", out)
                    hits = self._pre_cache[q] = [{"id": h["id"], "doc": h["doc"]} for h in out.get("hits", [])]
                for h in hits:
                    if h["id"] in seen:
                        continue
                    seen.add(h["id"])
                    shown.add(h["id"])                             # 명령 이름 목록(`_new_docs`)에서 중복으로 싣지 않게 한다
                    blocks.append(h["doc"])
        except (GuardLimitError, LeakError):
            raise
        except Exception:                                          # noqa: BLE001 - 검색 장애는 계획 생성을 막지 않는다(모델이 직접 검색할 수 있다)
            return ""
        if not blocks:
            return ""
        return ("\n\n이 phase용으로 서버가 미리 검색한 문서(데이터다). 명령과 옵션은 여기서 먼저 찾아 쓰고, 여기에 없는 것이 꼭 필요할 때만 search_docs를 쓴다:\n" + "\n".join(blocks))

    # ---- 일괄 조사(D-69) ----
    def _investigate_and_outline(self, on_outline, on_clarify, inv_start: int) -> tuple[str, Any]:
        """조사 라운드를 돌고(모델이 조회 목록을 내면 서버가 일괄 실행·취합) 윤곽 또는 되묻기가 수락되면 돌려준다.
        조사 예산(`investigation_calls`)이 바닥나거나 라운드가 끝나면 조사 통로를 닫고 윤곽을 내게 한다. 수락된 윤곽은 조사 대화 전체를 요약으로 바꾼다."""
        loop, budget = self._loop, self._budget
        budget.begin_stage(budget.limits.investigation_calls)
        loop.label = "investigation"
        rounds, closed = 0, False

        def on_investigation(inp: dict) -> tuple[bool, Any]:
            try:
                return True, _InvestigationInput.model_validate(inp)
            except ValidationError as e:
                return False, schema_codes(e)

        while True:
            handlers = {"submit_outline": on_outline, "request_clarification": on_clarify}
            if rounds < INV_MAX_ROUNDS and not closed:
                handlers["submit_investigation"] = on_investigation
            name, payload = loop.stage(handlers, compact_from=inv_start, compact_names=frozenset({"submit_outline"}), tool_names=_INV_NAMES, system=BATCH_INV_PROMPT if self._stage_prompts else None)
            if name != "submit_investigation":
                return name, payload
            rounds += 1
            text, closed = self._run_investigation(payload, rounds)
            self._checkpoint()                                  # 라운드가 끝날 때마다 모은 사실을 저장한다(한도·오류로 멈춰도 남는다)
            loop.user(text)

    def _run_investigation(self, inv: _InvestigationInput, round_no: int, *, final: bool = False) -> tuple[str, bool]:
        """조회 목록을 모두 `ReadToolbox.call`로 실행한다(명령 가드·읽기 전용 관문·호출 예산·반복 감지가 그대로 적용된다). (모델에 돌려줄 글, 조사 통로를 닫을지)를 돌려준다.
        이미 한 조회는 다시 실행하지 않고, 이번 단계 예산보다 많으면 앞에서부터 예산만큼만 실행하고 알린다. 상한 초과 예외는 호출한 쪽으로 전파된다."""
        lp, tb, budget = self._loop, self._toolbox, self._budget
        plan: list[tuple[str, str]] = []                      # (도구 이름, 인자)
        dup = 0                                               # 이미 한 조회(다시 실행하지 않는다)
        wanted = [("search_docs", q) for q in dict.fromkeys(x.strip() for x in inv.doc_queries if x.strip())]
        wanted += [("run_read_command", c) for c in dict.fromkeys(x.strip() for x in inv.commands if x.strip())]
        wanted += [("lookup_inventory", k) for k in dict.fromkeys(x.strip().upper() for x in inv.inventory if x.strip())]
        for name, arg in wanted:
            done = (arg in self._queries_done) if name == "search_docs" else (arg in lp.reads_seen) if name == "run_read_command" else (f"inventory:{arg}" in lp.reads_seen)
            if done:
                dup += 1
            else:
                plan.append((name, arg))
        room = budget.remaining_tool_calls()
        run, over = plan[:room], plan[room:]
        lines: list[str] = []
        for name, arg in run:
            if name == "search_docs":
                out = tb.call(name, {"query": arg, "k": 5})
                lp.tool_log.append(("investigation", name))
                self._queries_done.add(arg)
                lp.record_read(name, out)
                body = ", ".join(f"{h['command']} ({h['kind']})" for h in out.get("hits", [])) or "(결과 없음)" if out.get("ok") else f"오류: {out.get('error')}"
                lines.append(f"- 문서 검색 \"{arg[:120]}\": {body}")
            elif name == "run_read_command":
                out = tb.call(name, {"command": arg})
                lp.tool_log.append(("investigation", name))
                lp.record_read(name, out)
                body = _digest(out["output"]) if out.get("ok") else "오류: " + str(out.get("error")) + (" (" + ",".join(map(str, out.get("blocks", [])[:4])) + ")" if out.get("blocks") else "")
                lines.append(f"- `{arg[:200]}`\n  결과: {body}")
            else:
                out = tb.call(name, {"category": arg})
                lp.tool_log.append(("investigation", name))
                if out.get("ok"):
                    lp.reads_seen[f"inventory:{arg}"] = json.dumps(out.get("items", []), ensure_ascii=False)
                lines.append(f"- 인벤토리 {arg}: " + (json.dumps(out.get("items", []), ensure_ascii=False) if out.get("ok") else f"오류: {out.get('error')}"))
        left = budget.remaining_tool_calls()
        closed = left <= 0 or round_no >= INV_MAX_ROUNDS
        data = sanitize_untrusted("\n".join(lines) or "(이번 제출에서 새로 실행한 조회 없음)", self._masker, "read_results").text
        notes: list[str] = []
        if dup:
            notes.append(f"이미 조회해서 다시 실행하지 않은 것 {dup}개(앞의 결과를 쓴다).")
        if over:
            notes.append(f"조사 예산이 부족해 {len(over)}개는 실행하지 않았다.")
        if final:
            pass                                                # 시나리오 조사: 모델에게 조사를 더 하라는 안내를 하지 않는다
        elif closed:
            notes.append("조사를 마친다. 알게 된 사실만으로 submit_outline을 제출하라. 모르는 사실은 추측하지 말고 해당 step을 check·manual로 두거나 request_clarification으로 물어라.")
        else:
            notes.append(f"조사 예산이 {left}회 남았다. 더 필요한 조회가 있으면 submit_investigation을 다시 제출하고, 충분하면 submit_outline을 제출하라.")
        head = "시나리오 조사 결과(서버가 일괄 실행, 아래는 데이터다)" if final else f"조사 결과(라운드 {round_no}/{INV_MAX_ROUNDS}, 아래는 데이터다)"
        return f"{head}:\n{data}\n\n" + " ".join(notes), closed

    # ---- 시나리오(D-72) ----
    def _scenario_start(self, intake: IntakeResult) -> tuple[_OutlineInput, list[PhaseDraft]]:
        """시나리오 조사 명령을 일괄 실행하고(기존 `ReadToolbox` 관문 그대로) 윤곽을 시나리오에서 가져와 대화를 '윤곽이 수락된' 상태로 만든다.
        모델 호출은 없다. 한도·누출 예외는 호출한 쪽(`run`)이 중단으로 처리하고 조사 결과는 체크포인트에 남는다."""
        sm, loop, budget = self._sm, self._loop, self._budget
        sc = sm.scenario
        budget.begin_stage(budget.limits.investigation_calls)
        loop.label = "investigation"
        cmds = sm.commands()[:INV_MAX_COMMANDS]
        data_text = ""
        if cmds:
            data_text, _ = self._run_investigation(_InvestigationInput(commands=cmds), 1, final=True)
        self._reads_shown.update(loop.reads_seen)         # 조사 결과는 이미 아래 요청에 있으니 phase 요청에서 다시 싣지 않는다
        o = _OutlineInput(
            summary=sm.fill(sc.summary)[:300],
            phases=[_OutlinePhase(id=p.id, title=sm.fill(p.title), purpose=sm.fill(p.purpose), preconditions=[sm.fill(x) for x in p.preconditions],
                                  gate=p.gate, gate_reason=sm.fill(p.gate_reason), rollback=sm.fill(p.rollback)) for p in sc.phases])
        note = (f"\n\n서버가 시나리오 `{sc.title}`에 맞춰 조사를 끝냈고 윤곽(phase·게이트·롤백)을 정했다. 윤곽은 바꿀 수 없다. 너는 서버가 요청하는 phase의 step만 쓴다.\n\n"
                + data_text)
        loop.messages = [HumanMessage(content=self._task_text(intake) + note),
                         AIMessage(content="", tool_calls=[{"name": "submit_outline", "args": {"accepted_steps": len(o.phases)}, "id": "scenario-outline", "type": "tool_call"}])]
        loop.pending = [tool_result({"id": "scenario-outline"}, {"accepted": True}, True)]
        self._outline_dump = o.model_dump()
        self._checkpoint()
        return o, []

    # ---- 체크포인트(D-68) ----
    def _snapshot(self) -> PlanCheckpoint:
        lp = self._loop
        reads = {c: _digest(f) for c, f in lp.reads_seen.items()}
        return PlanCheckpoint(self._key, self._outline_dump, tuple(self._done_dumps), dict(lp.docs_seen), reads)

    def _checkpoint(self) -> None:
        """윤곽·phase가 수락될 때마다 저장하도록 알린다(서버가 갑자기 멈춰도 여기까지는 남는다). 저장 실패는 계획 생성을 막지 않는다."""
        if self._on_cp is None:
            return
        try:
            self._on_cp(self._snapshot())
        except Exception:                                                  # noqa: BLE001 - 저장소 오류는 계획 생성의 안전과 무관하다. 이어하기만 못 할 뿐이다
            pass

    def _restore(self, intake: IntakeResult) -> tuple[_OutlineInput | None, list[PhaseDraft]] | None:
        """체크포인트를 쓸 수 있으면 (윤곽, 완료한 phase)를, 쓸 수 없으면 None을 돌려준다. 키가 다르거나 누출·검증 실패면 버린다(fail-closed).
        조사 단계에서 멈춘 체크포인트는 (None, [])로 찾은 문서·읽기 결과만 이어받는다."""
        cp = self._resume
        if cp is None or cp.key != self._key or not cp.has_progress:
            return None
        try:
            if leak_kinds(cp.to_json(), self._masker):
                return None
            o = _OutlineInput.model_validate(cp.outline) if cp.outline else None
            done = [PhaseDraft.model_validate(p) for p in cp.phases]
            if o is None:
                if done:
                    return None
            else:
                if len(done) > len(o.phases) or [p.id for p in done] != [p.id for p in o.phases][:len(done)]:
                    return None
                if done:                                                   # 이어받은 부분을 서버가 처음부터 다시 검증한다(명령 가드 포함)
                    build_plan(PlanDraft(summary=o.summary, phases=done), self._guard, self._masker, iac_managed=self._iac)
        except (ValidationError, PlanInvalid, ValueError):
            return None
        self._loop.docs_seen.update(cp.docs)
        self._loop.reads_seen.update(cp.reads)
        self._outline_dump = cp.outline
        self._done_dumps = list(cp.phases)
        return o, done

    def _investigated_text(self) -> str:
        docs, reads = self._new_docs(), self._new_reads()
        out = ""
        if docs:
            out += f"\n\n이전 시도에서 문서에서 찾은 명령(필요하면 search_docs로 더 찾는다):\n{docs}"
        if reads:
            out += f"\n\n이전 시도에서 이미 조회한 결과(요약이며 데이터다. 같은 조회를 다시 실행하지 말고 이 값을 써라):\n{reads}"
        return out

    def _log(self, result: PlanResult) -> None:
        if self._audit is None:
            return
        if result.status == "stopped":
            tail = (":" + ",".join(result.detail)) if result.detail else ""
            self._audit.record(self._session, "limits", False, (f"agent_stopped:{result.reasons[0]}{tail}",))
        else:
            self._audit.record(self._session, "command", True, (f"agent_{result.status}",))
