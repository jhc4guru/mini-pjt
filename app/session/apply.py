"""apply 진행 엔진: 승인된 step을 실행기로 보내고, 확인 step을 자동 실행하고, 실패하면 멈춰 사람의 선택을 받는다.

- 실행 직전에 승인 자격을 저장된 기록에서 다시 계산한다(`ApprovalEngine.grant_for`). 실행은 `Executor.execute`(단일 관문) 한 곳이다.
- step을 `running`으로 바꾸는 것이 실행 선점이다(`expect="approved"`). 같은 step의 동시 실행 요청은 하나만 통과한다.
- 실행 결과는 마스킹하고 시크릿을 지운 텍스트만 저장한다. 원문은 저장하지 않는다.
- 실패하면 세션이 `paused`가 되고(다른 step 승인 불가) 실패 분석(선택)이 추천을 남긴다. 선택은 사람이 하고 서버 규칙이 한 번 더 막는다:
  재시도는 일시적인 오류일 때만, 시도 횟수 상한 이내, 확인 필요 step은 상태 확인 보고가 있어야 한다. 건너뛰기는 요청자가 아닌 사람이,
  의존하는 step이 없을 때만 한다. 모든 선택은 이벤트와 감사 기록에 남는다.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from app.executor.base import ExecutionRequest, ExecutionResult, Executor
from app.guards.audit import AuditLog
from app.guards.command_guard import CommandGuard
from app.guards.ref_resolve import RefError, resolve as resolve_refs
from app.guards.refs import find_refs
from app.guards.input_guard import check_user_input
from app.guards.execution_policy import ExecutionDenied, assert_read_only_tool_call
from app.guards.agent_limits import ToolNotAllowed
from app.guards.masking import Masker
from app.guards.patterns import SECRET_PATTERNS
from app.planner.failure import _RETRYABLE, MAX_RETRY_ATTEMPTS, FailureAnalysis, classify_error
from app.planner.schema import Plan, Step
from app.session import states
from app.session.approval import AUTO, ApprovalEngine, ApprovalError
from app.session.store import SessionStore

SECRET_MARK = "[비밀값 제거됨]"
DEFAULT_TIMEOUT = 120.0
CHOICES = ("retry", "skip", "abort")

Analyzer = Callable[[Plan, str, ExecutionResult, int, str], FailureAnalysis]


def clean_output(text: str, masker: Masker, limit: int = 20000) -> str:
    """실행 결과를 저장·표시하기 전에 마스킹하고 시크릿을 지운다."""
    text = masker.mask((text or "")[:limit * 2])
    for pat in SECRET_PATTERNS.values():
        text = pat.sub(SECRET_MARK, text)
    return text[:limit]


@dataclass(frozen=True)
class StepOutcome:
    step_id: str
    state: str                              # done | failed
    status: str = ""                        # ok | failed | timeout | denied
    exit_code: int | None = None
    output: str = ""                        # 마스킹·정리된 텍스트
    hint: str = ""
    analysis: dict | None = None            # 실패 분석 요약(있으면)
    checks: tuple["StepOutcome", ...] = ()  # 자동 실행된 확인 step 결과


class ApplyEngine:
    def __init__(self, store: SessionStore, approvals: ApprovalEngine, executor: Executor, guard: CommandGuard,
                 masker_for: Callable[[str], Masker], *, analyzer: Analyzer | None = None, audit: AuditLog | None = None,
                 clock: Callable[[], float] = time.time, on_event: Callable[[str, str, dict], None] | None = None):
        self._s, self._ap, self._ex, self._guard, self._masker_for = store, approvals, executor, guard, masker_for
        self._analyzer, self._audit, self._clock = analyzer, audit, clock
        self._on_event = on_event          # (sid, 종류, 내용): 실패·분석 시작/끝을 화면에 알리는 데 쓴다(값 없음)

    # ---------------- 실행 ----------------
    def run_step(self, sid: str, version: int, step_id: str, *, nick: str) -> StepOutcome:
        """`approved` 상태인 step을 실행한다. 변경 step이 성공하면 그 확인 step도 자동으로 실행한다."""
        sess = self._s.session(sid)
        if sess["state"] != "applying":
            raise self._deny(sid, f"not_applying:{sess['state']}", nick)
        if nick != AUTO and self._s.role_of(sid, nick) is None:
            raise self._deny(sid, "not_a_participant", nick)
        plan = self._s.load_plan(sid, version)                       # 해시 재검증
        step = self._find(plan, step_id)
        if step.op not in ("read", "mutate") or not step.command:
            raise self._deny(sid, "step_not_server_executable", nick)
        if step.kind == "poll":
            raise self._deny(sid, "poll_step_uses_poll_api", nick)
        grant = self._ap.grant_for(sid, version, step_id)            # 저장된 승인 기록에서 재계산
        masker = self._masker_for(sid)
        attempts = self._s.step_states(sid, version)[step_id]["attempts"] + 1
        self._s.transition_step(sid, version, step_id, "running", expect="approved", nick=nick, count_attempt=True)      # 실행 선점
        try:
            result = self._exec(sid, version, step, grant, masker)
        except ExecutionDenied as e:
            return self._failed(sid, version, plan, step, nick, attempts, None, f"denied:{e.code}", "denied", "")
        raw = f"{result.stderr}\n{result.stdout}"
        text = clean_output(result.stdout if result.status == "ok" else (result.stderr or result.stdout), masker)
        if result.status == "ok":
            self._s.add_output(sid, version, step_id, attempts, "ok", result.exit_code, text)
            self._s.transition_step(sid, version, step_id, "done", detail="ok", nick=nick)
            self._audit_rec(sid, True, "step_done")
            checks = self._run_checks(sid, version, plan, step, nick) if step.op == "mutate" else ()
            if checks and checks[-1].state == "failed":
                return StepOutcome(step_id, "done", "ok", result.exit_code, text, checks=checks)
            self._complete_if_done(sid, version, plan, nick)
            return StepOutcome(step_id, "done", "ok", result.exit_code, text, checks=checks)
        return self._failed(sid, version, plan, step, nick, attempts, result, result.status, result.status, text, raw=raw)

    def _run_checks(self, sid: str, version: int, plan: Plan, step: Step, nick: str) -> tuple[StepOutcome, ...]:
        """끝난 step의 확인(읽기) step을 자동 실행한다. 확인 step이 poll이면 조회를 시작하고(완료는 사람이 확인),
        자동 승인 조건(의존·앞선 phase)이 맞지 않으면 실행하지 않고 사람이 승인하도록 `pending`으로 둔다."""
        out = []
        for c in plan.all_steps():
            if c.check_for == step.id and c.op == "read":
                if self._s.step_states(sid, version)[c.id]["state"] != "pending":
                    continue
                try:
                    self._ap.auto_approve_check(sid, version, c.id)
                except ApprovalError:
                    continue
                o = self.poll_once(sid, version, c.id, nick=AUTO) if c.kind == "poll" else self.run_step(sid, version, c.id, nick=AUTO)
                out.append(o)
                if o.state == "failed":
                    break
        return tuple(out)

    def _failed(self, sid: str, version: int, plan: Plan, step: Step, nick: str, attempt: int, result: ExecutionResult | None,
                detail: str, status: str, text: str, *, raw: str = "") -> StepOutcome:
        hint = "timeout" if status == "timeout" else classify_error(raw or detail)
        self._s.add_output(sid, version, step.id, attempt, status, result.exit_code if result else None, text or detail, hint=hint)
        self._s.transition_step(sid, version, step.id, "failed", detail=detail[:200], nick=nick)
        if self._s.session(sid)["state"] == "applying":
            self._s.set_session_state(sid, "paused", nick=nick)           # 실패하면 멈춘다(사람이 재시도·건너뛰기·중단을 고른다)
        self._audit_rec(sid, False, f"step_failed:{status}:{hint}")
        will_analyze = self._analyzer is not None and result is not None
        self._emit(sid, "failed", {"step_id": step.id, "hint": hint, "status": status, "analyzing": will_analyze})      # 분석을 시작하기 전에 알린다("답변 생성 중" 표시)
        analysis = self._analyze(sid, version, plan, step, result, attempt, nick)
        if will_analyze:
            self._emit(sid, "analysis", {"step_id": step.id, "summary": analysis})
        return StepOutcome(step.id, "failed", status, result.exit_code if result else None, text, hint, analysis)

    def _analyze(self, sid: str, version: int, plan: Plan, step: Step, result: ExecutionResult | None, attempt: int, nick: str) -> dict | None:
        if self._analyzer is None or result is None:
            return None
        try:
            a = self._analyzer(plan, step.id, result, attempt, sid)
        except Exception as e:                                          # 분석 실패는 진행을 막지 않는다(사람이 직접 고른다)
            self._s.log(sid, "failure_analysis_error", type(e).__name__, nick=nick)
            return None
        masker = self._masker_for(sid)
        summary = {"step": step.id, "status": a.status, "recommendation": a.recommendation, "category": a.category, "server_hint": a.server_hint,
                   "adjustments": list(a.adjustments), "needs_state_check": a.needs_state_check, "check_step_ids": list(a.check_step_ids),
                   "cause": clean_output(a.cause, masker, 400), "alternatives": [s.id for s in a.alternatives], "reasons": list(a.reasons)}
        self._s.log(sid, "failure_analysis", json.dumps(summary, ensure_ascii=False)[:480], nick=nick)
        return summary

    def _exec(self, sid: str, version: int, step: Step, grant, masker: Masker) -> ExecutionResult:
        """승인 자격과 함께 실행기 관문(`Executor.execute`)으로 보낸다. 서버가 실행하는 유일한 호출 지점이다."""
        command = self._resolved(sid, version, step, masker)             # 앞 절차 결과 참조(D-48)가 있으면 서버가 리소스 ID만 꺼내 치환한다
        verdict = self._guard.check(command, masker)                    # 치환한 명령을 가드가 다시 검사한다
        request = ExecutionRequest.create(kind=step.op, masked_command=command, command=masker.unmask(command),
                                          session_id=sid, plan_id=f"{sid}:v{version}", step_id=step.id, timeout_seconds=DEFAULT_TIMEOUT)
        return self._ex.execute(request, verdict=verdict, approved_by_other=grant.approved_by_other, confirmations=grant.confirmations)

    def _resolved(self, sid: str, version: int, step: Step, masker: Masker) -> str:
        """참조가 없으면 plan의 명령 그대로. 있으면 참조한 절차가 이 plan에서 끝났고(done) 성공 출력이 있을 때만 치환하며, 치환 결과(마스킹된 명령)를 이벤트로 남긴다."""
        if not find_refs(step.command) and "@{" not in (step.command or ""):
            return step.command
        states = self._s.step_states(sid, version)

        def output_of(ref_id: str) -> str:
            if states.get(ref_id, {}).get("state") != "done":
                raise RefError("step_not_done")
            ok = [o for o in self._s.outputs(sid, version, ref_id) if o["status"] == "ok"]
            if not ok:
                raise RefError("no_output")
            return ok[-1]["text"]
        try:
            resolved = resolve_refs(step.command, output_of, masker)
        except RefError as e:
            self._s.log(sid, "step_ref_failed", f"{step.id}: {e.code}", nick=AUTO)
            raise
        self._s.log(sid, "step_ref_resolved", f"{step.id}: {resolved}"[:480], nick=AUTO)
        return resolved

    # ---------------- manual·iac: 사람이 실행하고 보고한다(서버는 실행하지 않는다) ----------------
    def report_done(self, sid: str, nick: str, version: int, step_id: str, *, text: str = "", checked: tuple[int, ...] | list[int] = ()) -> None:
        """요청자가 "실행했다"고 보고한다. 결과 붙여넣기는 선택이며 입력 가드와 마스킹을 거친다. iac는 체크리스트를 모두 표시해야 한다."""
        sess, plan, step = self._report_ctx(sid, nick, version, step_id, "awaiting_report")
        if nick.lower() != sess["requester"].lower():
            raise self._deny(sid, "report_by_requester_only", nick)
        if step.checklist and set(checked) != set(range(len(step.checklist))):
            raise self._deny(sid, "checklist_incomplete", nick)
        masked = self._clean_user_text(sid, nick, text)
        self._s.add_approval(sid, version, "report", step_id, nick, self._s.plan_meta(sid, version)["plan_hash"], masked or "reported")
        if masked:
            self._s.add_output(sid, version, step_id, 0, "reported", None, masked)
        self._s.transition_step(sid, version, step_id, "awaiting_confirm", detail="reported", nick=nick, count_attempt=True, expect="awaiting_report")
        self._audit_rec(sid, True, "report_done")

    def report_failed(self, sid: str, nick: str, version: int, step_id: str, *, text: str = "") -> None:
        sess, plan, step = self._report_ctx(sid, nick, version, step_id, "awaiting_report")
        if nick.lower() != sess["requester"].lower():
            raise self._deny(sid, "report_by_requester_only", nick)
        masked = self._clean_user_text(sid, nick, text)
        self._s.add_output(sid, version, step_id, 0, "reported_failed", None, masked or "(내용 없음)", hint="unknown")
        self._s.transition_step(sid, version, step_id, "failed", detail="reported_failed", nick=nick, count_attempt=True, expect="awaiting_report")
        self._s.set_session_state(sid, "paused", nick=nick)
        self._audit_rec(sid, False, "report_failed")

    def confirm_report(self, sid: str, nick: str, version: int, step_id: str, *, accept: bool = True, note: str = "") -> None:
        """요청자가 아닌 사람이 보고를 확인한다. 거절하면 다시 보고를 기다린다."""
        sess, plan, step = self._report_ctx(sid, nick, version, step_id, "awaiting_confirm")
        if nick.lower() == sess["requester"].lower():
            raise self._deny(sid, "requester_cannot_confirm_report", nick)
        masked = self._clean_user_text(sid, nick, note)
        if not accept:
            self._s.revoke_approvals(sid, version, "report", step_id, nick=nick)
            self._s.transition_step(sid, version, step_id, "awaiting_report", detail=("rejected: " + masked)[:200], nick=nick, expect="awaiting_confirm")
            self._audit_rec(sid, False, "report_rejected")
            return
        self._s.add_approval(sid, version, "verify", step_id, nick, self._s.plan_meta(sid, version)["plan_hash"], masked)
        self._s.transition_step(sid, version, step_id, "done", detail="confirmed", nick=nick, expect="awaiting_confirm")
        self._audit_rec(sid, True, "report_confirmed")
        self._run_checks(sid, version, plan, step, nick)
        self._complete_if_done(sid, version, plan, nick)

    def _report_ctx(self, sid: str, nick: str, version: int, step_id: str, expect_state: str):
        sess = self._s.session(sid)
        if self._s.role_of(sid, nick) is None:
            raise self._deny(sid, "not_a_participant", nick)
        if sess["state"] != "applying":
            raise self._deny(sid, f"not_applying:{sess['state']}", nick)
        plan = self._s.load_plan(sid, version)
        step = self._find(plan, step_id)
        if step.op not in ("manual", "none"):
            raise self._deny(sid, "not_a_reported_step", nick)
        st = self._s.step_states(sid, version)[step_id]["state"]
        if st != expect_state:
            raise self._deny(sid, f"step_not_{expect_state}:{st}", nick)
        return sess, plan, step

    def _clean_user_text(self, sid: str, nick: str, text: str) -> str:
        if not text or not text.strip():
            return ""
        v = check_user_input(text, self._masker_for(sid))
        if not v.allow or v.masked_text is None:
            raise self._deny(sid, "text_blocked:" + ",".join(v.reasons[:2]), nick)
        return v.masked_text

    # ---------------- poll: 서버가 간격을 두고 읽기 명령을 반복한다. 완료는 사람이 결과를 보고 확인한다 ----------------
    def poll_once(self, sid: str, version: int, step_id: str, *, nick: str) -> StepOutcome:
        """poll step을 한 번 조회한다. 첫 조회에서 실행을 선점하고 시작 시각을 남긴다. 최대 시간을 넘기면 실패로 멈춘다."""
        sess = self._s.session(sid)
        if sess["state"] != "applying":
            raise self._deny(sid, f"not_applying:{sess['state']}", nick)
        if nick != AUTO and self._s.role_of(sid, nick) is None:
            raise self._deny(sid, "not_a_participant", nick)
        plan = self._s.load_plan(sid, version)
        step = self._find(plan, step_id)
        if step.kind != "poll" or step.op != "read" or step.poll is None or not step.command:
            raise self._deny(sid, "not_a_poll_step", nick)
        grant = self._ap.grant_for(sid, version, step_id)
        st = self._s.step_states(sid, version)[step_id]
        masker = self._masker_for(sid)
        now = self._clock()
        if st["state"] == "approved":
            self._s.transition_step(sid, version, step_id, "running", expect="approved", detail=f"poll_started={now:.0f}", nick=nick, count_attempt=True)
            started = now
        elif st["state"] == "running" and st["detail"].startswith("poll_started="):
            started = float(st["detail"].split("=", 1)[1])
        else:
            raise self._deny(sid, f"poll_not_runnable:{st['state']}", nick)
        n = len([o for o in self._s.outputs(sid, version, step_id) if o["status"].startswith("poll")]) + 1
        attempts = self._s.step_states(sid, version)[step_id]["attempts"]
        if now - started > step.poll.max_seconds:
            return self._failed(sid, version, plan, step, nick, attempts, None, "poll_timeout", "timeout", f"poll {n}: 최대 시간 초과")
        try:
            result = self._exec(sid, version, step, grant, masker)
        except ExecutionDenied as e:
            return self._failed(sid, version, plan, step, nick, attempts, None, f"denied:{e.code}", "denied", "")
        text = clean_output(result.stdout if result.status == "ok" else (result.stderr or result.stdout), masker)
        if result.status != "ok":
            return self._failed(sid, version, plan, step, nick, attempts, result, result.status, result.status, text, raw=f"{result.stderr}\n{result.stdout}")
        self._s.add_output(sid, version, step_id, attempts, f"poll:{n}", result.exit_code, text)
        return StepOutcome(step_id, "running", "ok", result.exit_code, text)

    def poll_complete(self, sid: str, nick: str, version: int, step_id: str) -> None:
        """사람이 최신 조회 결과를 보고 완료 조건(`until`)이 충족됐음을 확인한다. 조회 결과가 한 번도 없으면 확인할 수 없다."""
        sess = self._s.session(sid)
        if self._s.role_of(sid, nick) is None:
            raise self._deny(sid, "not_a_participant", nick)
        if sess["state"] != "applying":
            raise self._deny(sid, f"not_applying:{sess['state']}", nick)
        plan = self._s.load_plan(sid, version)
        step = self._find(plan, step_id)
        if step.kind != "poll":
            raise self._deny(sid, "not_a_poll_step", nick)
        if self._s.step_states(sid, version)[step_id]["state"] != "running":
            raise self._deny(sid, "poll_not_running", nick)
        if not any(o["status"].startswith("poll:") for o in self._s.outputs(sid, version, step_id)):
            raise self._deny(sid, "no_poll_result_yet", nick)
        self._s.add_approval(sid, version, "verify", step_id, nick, self._s.plan_meta(sid, version)["plan_hash"], "poll_condition_met")
        self._s.transition_step(sid, version, step_id, "done", detail="poll_complete", nick=nick, expect="running")
        self._audit_rec(sid, True, "poll_complete")
        self._complete_if_done(sid, version, plan, nick)

    def run_poll(self, sid: str, version: int, step_id: str, *, nick: str, sleep: Callable[[float], None] = time.sleep,
                 max_polls: int = 10000) -> StepOutcome:
        """완료되거나 실패하거나 중단될 때까지 간격을 두고 조회한다. 서버가 재시작되면 `recover_after_restart`의 resumable 목록으로 다시 호출한다."""
        plan = self._s.load_plan(sid, version)
        step = self._find(plan, step_id)
        out = self.poll_once(sid, version, step_id, nick=nick)
        for _ in range(max_polls):
            if out.state != "running" or self._s.step_states(sid, version)[step_id]["state"] != "running" or self._s.session(sid)["state"] != "applying":
                break
            sleep(step.poll.interval_seconds)
            if self._s.step_states(sid, version)[step_id]["state"] != "running" or self._s.session(sid)["state"] != "applying":
                break
            out = self.poll_once(sid, version, step_id, nick=nick)
        return out

    # ---------------- 서버 재시작 ----------------
    def recover_after_restart(self, sid: str, version: int, *, nick: str = AUTO) -> dict[str, list[str]]:
        """재시작 후 `running`으로 남은 step을 정리한다. poll은 이어서 조회하고(`resumable`), 그 밖에는 실행 여부를 알 수 없으므로
        실패(`interrupted`)로 멈춰 사람이 상태를 확인하고 고르게 한다(변경 step은 재시도 전에 상태 확인이 필요하다)."""
        out: dict[str, list[str]] = {"resumable": [], "interrupted": []}
        if self._s.session(sid)["state"] != "applying":
            return out
        plan = self._s.load_plan(sid, version)
        for step_id, st in self._s.step_states(sid, version).items():
            if st["state"] != "running":
                continue
            step = self._find(plan, step_id)
            if step.kind == "poll" and st["detail"].startswith("poll_started="):
                out["resumable"].append(step_id)
                continue
            self._s.add_output(sid, version, step_id, st["attempts"], "interrupted", None, "서버 재시작으로 실행 결과를 알 수 없다", hint="unknown")
            self._s.transition_step(sid, version, step_id, "failed", detail="interrupted", nick=nick)
            if self._s.session(sid)["state"] == "applying":
                self._s.set_session_state(sid, "paused", nick=nick)
            out["interrupted"].append(step_id)
        return out

    def _emit(self, sid: str, kind: str, data: dict) -> None:
        if self._on_event is not None:
            try:
                self._on_event(sid, kind, data)
            except Exception:                                          # 알림 실패가 실행 흐름을 막지 않는다
                pass

    # ---------------- 실패 후 선택 ----------------
    def decide(self, sid: str, nick: str, version: int, step_id: str, choice: str, *, state_checked: bool = False) -> str:
        """실패한 step에 대해 재시도, 건너뛰기, 중단을 고른다. 서버 규칙이 허용할 때만 적용한다."""
        if choice not in CHOICES:
            raise self._deny(sid, "unknown_choice", nick)
        if self._s.role_of(sid, nick) is None:
            raise self._deny(sid, "not_a_participant", nick)
        sess = self._s.session(sid)
        if sess["state"] != "paused":
            raise self._deny(sid, f"not_paused:{sess['state']}", nick)
        plan = self._s.load_plan(sid, version)
        step = self._find(plan, step_id)
        st = self._s.step_states(sid, version)[step_id]
        if st["state"] != "failed":
            raise self._deny(sid, f"step_not_failed:{st['state']}", nick)
        if choice == "abort":
            self._abort_all(sid, version, nick)
            return "aborted"
        if choice == "retry":
            out = self._s.outputs(sid, version, step_id)
            hint = out[-1]["hint"] if out else "unknown"
            if out and out[-1]["status"] == "denied":
                raise self._deny(sid, "retry_not_allowed:denied", nick)               # 관문이 거부한 실행은 같은 조건으로 다시 하지 않는다
            if hint not in _RETRYABLE:
                raise self._deny(sid, f"retry_not_allowed:{hint}", nick)
            if st["attempts"] >= MAX_RETRY_ATTEMPTS:
                raise self._deny(sid, "retry_limit_reached", nick)
            if step.op == "mutate" and (hint in ("timeout", "unknown") or step.irreversible) and not state_checked:
                raise self._deny(sid, "state_check_required", nick)                    # 이미 적용됐을 수 있으니 상태를 먼저 확인한다
            self._s.transition_step(sid, version, step_id, "pending", detail="retry", nick=nick)
            self._ap.reset_step_approvals(sid, version, step_id, nick=nick)
            if step.check_for and step.op == "read":
                self._s.set_session_state(sid, "applying", nick=nick)
                self._ap.auto_approve_check(sid, version, step_id)                    # 확인 step 재시도는 자동 승인(실행은 호출자가)
            else:
                self._s.set_session_state(sid, "applying", nick=nick)
            self._audit_rec(sid, True, "retry_chosen")
            return "retry"
        # skip
        if nick.lower() == sess["requester"].lower():
            raise self._deny(sid, "requester_cannot_skip", nick)
        deps = [s.id for s in plan.all_steps() if step_id in s.depends_on and s.check_for != step_id]
        if deps:
            raise self._deny(sid, f"skip_has_dependents:{','.join(deps[:4])}", nick)
        self._s.transition_step(sid, version, step_id, "skipped", detail="skipped", nick=nick)
        self._s.set_session_state(sid, "applying", nick=nick)
        self._audit_rec(sid, True, "skip_chosen")
        self._complete_if_done(sid, version, plan, nick)
        return "skip"

    def abort(self, sid: str, nick: str, version: int) -> None:
        if self._s.role_of(sid, nick) is None:
            raise self._deny(sid, "not_a_participant", nick)
        if self._s.session(sid)["state"] not in ("applying", "paused", "plan_approved"):
            raise self._deny(sid, "cannot_abort", nick)
        self._abort_all(sid, version, nick)

    def _abort_all(self, sid: str, version: int, nick: str) -> None:
        for step_id, st in self._s.step_states(sid, version).items():
            if st["state"] not in states.TERMINAL_STEP and st["state"] != "running":
                self._s.transition_step(sid, version, step_id, "aborted", detail="abort", nick=nick)
        self._s.set_session_state(sid, "aborted", nick=nick)
        self._audit_rec(sid, True, "aborted")

    # ---------------- plan 검토 중 읽기 ----------------
    def preview_read(self, sid: str, version: int, step_id: str, *, nick: str) -> StepOutcome:
        """plan 검토 중에 읽기 step을 실행해 결과를 보여 준다. 문서에 근거한 읽기 명령만 가능하고 변경 step은 실행할 수 없다."""
        if self._s.role_of(sid, nick) is None:
            raise self._deny(sid, "not_a_participant", nick)
        if self._s.session(sid)["state"] not in ("plan_review", "plan_approved"):
            raise self._deny(sid, "preview_not_allowed", nick)
        plan = self._s.load_plan(sid, version)
        step = self._find(plan, step_id)
        masker = self._masker_for(sid)
        if step.op != "read" or not step.command:
            raise self._deny(sid, "preview_read_only", nick)
        if "@{" in step.command:
            raise self._deny(sid, "preview_has_references", nick)          # 앞 절차 결과를 쓰는 명령은 그 절차가 실행되기 전에는 미리 볼 수 없다
        verdict = self._guard.check(step.command, masker)
        try:
            assert_read_only_tool_call(verdict)
        except ToolNotAllowed:
            raise self._deny(sid, "preview_not_allowed_for_step", nick) from None
        request = ExecutionRequest.create(kind="read", masked_command=step.command, command=masker.unmask(step.command),
                                          session_id=sid, plan_id=f"{sid}:v{version}", step_id=step_id, timeout_seconds=DEFAULT_TIMEOUT)
        try:
            result = self._ex.execute(request, verdict=verdict)
        except ExecutionDenied as e:
            raise self._deny(sid, f"preview_denied:{e.code}", nick) from None
        text = clean_output(result.stdout if result.status == "ok" else (result.stderr or result.stdout), masker)
        self._s.add_output(sid, version, step_id, 0, "preview:" + result.status, result.exit_code, text)
        return StepOutcome(step_id, "preview", result.status, result.exit_code, text)

    # ---------------- 공통 ----------------
    def _complete_if_done(self, sid: str, version: int, plan: Plan, nick: str) -> None:
        st = self._s.step_states(sid, version)
        if all(v["state"] in states.SATISFIED_STEP for v in st.values()) and self._s.session(sid)["state"] == "applying":
            self._s.set_session_state(sid, "completed", nick=nick)
            self._audit_rec(sid, True, "completed")

    @staticmethod
    def _find(plan: Plan, step_id: str) -> Step:
        for s in plan.all_steps():
            if s.id == step_id:
                return s
        raise ApprovalError("unknown_step")

    def _deny(self, sid: str, code: str, nick: str | None) -> ApprovalError:
        self._audit_rec(sid, False, code)
        self._s.log(sid, "apply_denied", code, nick=nick)
        return ApprovalError(code)

    def _audit_rec(self, sid: str, allowed: bool, code: str) -> None:
        if self._audit is not None:
            self._audit.record(sid, "execution", allowed, (code,))
