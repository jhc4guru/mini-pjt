"""승인 엔진: plan 일괄 승인(해시 잠금), step별 4-eyes 승인, `verdict.requires` 조치, phase 게이트.

규칙(모두 서버가 정한다. 화면·모델의 주장은 믿지 않는다):
- plan 승인은 요청자가 아닌 참여자만 한다. 승인하는 해시가 저장된 최신 버전의 해시와 같아야 하고, 저장된 plan을 다시 읽어 해시를 재계산한다.
- 계획을 고치면 새 버전이 되어 이전 승인은 효력이 없다. apply 중에는 고칠 수 없다(일시정지 후 plan 복귀, 재승인).
- step 승인: 변경·폴백(AI 추정) step은 요청자가 아닌 사람만 승인한다. 앞선 phase가 끝나야 하고, 의존 step이 끝나야 하며,
  게이트 phase는 게이트 승인이 있어야 한다. `requires`가 모두 충족되어야 승인된다.
- 실행 직전에는 `grant_for`로 저장된 승인 기록에서 승인 자격(`StepGrant`)을 다시 계산한다(메모리의 승인 상태를 믿지 않는다).
- 모든 허용·거부는 `AuditLog`(stage `approval`)에 사유 코드로 남는다. 사용자 입력 텍스트(사유 등)는 입력 가드와 마스킹을 거친 뒤에만 저장한다.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Callable

from app.guards.audit import AuditLog
from app.guards.input_guard import check_user_input
from app.guards.masking import Masker
from app.planner.schema import Plan, Step
from app.session import states
from app.session.store import SessionStore, StoreError

DRY_RUN_TOKENS = ("--dryrun", "--dry-run")
CONFIRMER_REQUIRED = frozenset({"strong_warning", "prod_warning", "show_file_contents"})   # 요청자가 아닌 사람이 확인한다
MIN_REASON = 5
AUTO = "@auto"                  # 서버의 자동 승인 표시(닉네임 규칙상 사람이 쓸 수 없다)


class ApprovalError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class StepGrant:
    """실행기에 넘기는 승인 자격. 승인 기록에서 매번 다시 계산한다."""
    step_id: str
    plan_hash: str
    approved_by_other: bool
    confirmations: frozenset[str]
    approvers: tuple[str, ...]


def _base_tokens(command: str) -> list[str] | None:
    try:
        toks = shlex.split(command)
    except ValueError:
        return None
    return [t for t in toks if not t.startswith(DRY_RUN_TOKENS)]


def _has_dry_run(command: str) -> bool:
    try:
        return any(t.startswith(DRY_RUN_TOKENS) for t in shlex.split(command))
    except ValueError:
        return False


class ApprovalEngine:
    def __init__(self, store: SessionStore, masker_for: Callable[[str], Masker], *, audit: AuditLog | None = None):
        self._s, self._masker_for, self._audit = store, masker_for, audit

    # ---------------- 공통 ----------------
    def _deny(self, sid: str, code: str, nick: str | None = None) -> ApprovalError:
        if self._audit is not None:
            self._audit.record(sid, "approval", False, (code,))
        self._s.log(sid, "approval_denied", code, nick=nick)
        return ApprovalError(code)

    def _allow(self, sid: str, code: str) -> None:
        if self._audit is not None:
            self._audit.record(sid, "approval", True, (code,))

    def _member(self, sid: str, nick: str) -> str:
        role = self._s.role_of(sid, nick)
        if role is None:
            raise self._deny(sid, "not_a_participant", nick)
        return role

    def _current(self, sid: str, version: int) -> tuple[dict, Plan, dict]:
        sess = self._s.session(sid)
        meta = self._s.plan_meta(sid, version)
        return sess, self._s.load_plan(sid, version), meta       # 해시를 다시 계산해 바뀐 계획이면 PlanTampered

    @staticmethod
    def _find(plan: Plan, step_id: str) -> tuple[int, Step]:
        for i, p in enumerate(plan.phases):
            for s in p.steps:
                if s.id == step_id:
                    return i, s
        raise ApprovalError("unknown_step")

    # ---------------- plan ----------------
    def submit_plan(self, sid: str, nick: str, plan: Plan, *, parent_version: int | None = None, carry: dict[str, str] | None = None) -> int:
        """검토할 plan 버전을 올린다. apply 중에는 올릴 수 없다(일시정지 후 plan으로 돌아와야 한다)."""
        self._member(sid, nick)
        state = self._s.session(sid)["state"]
        if state in ("applying", "completed", "aborted"):
            raise self._deny(sid, f"plan_edit_not_allowed:{state}", nick)
        v = self._s.add_plan_version(sid, plan, parent_version=parent_version, nick=nick)
        self._s.init_steps(sid, v, [s.id for s in plan.all_steps()], carry=carry)
        if state != "plan_review":
            self._s.set_session_state(sid, "plan_review", nick=nick)
        self._allow(sid, "plan_submitted")
        return v

    def approve_plan(self, sid: str, nick: str, version: int, plan_hash_seen: str) -> None:
        """요청자가 아닌 참여자가 본 plan의 해시로 일괄 승인한다. 승인과 동시에 해시가 잠긴다."""
        self._member(sid, nick)
        sess, plan, meta = self._current(sid, version)
        if sess["state"] != "plan_review":
            raise self._deny(sid, f"plan_not_in_review:{sess['state']}", nick)
        if nick.lower() == sess["requester"].lower():
            raise self._deny(sid, "requester_cannot_approve_plan", nick)
        if version != self._s.latest_version(sid) or meta["status"] != "draft":
            raise self._deny(sid, "stale_plan_version", nick)
        if plan_hash_seen != meta["plan_hash"]:
            raise self._deny(sid, "plan_hash_mismatch", nick)
        self._s.add_approval(sid, version, "plan", "plan", nick, meta["plan_hash"])
        self._s.set_plan_status(sid, version, "approved", nick=nick)
        self._s.set_session_state(sid, "plan_approved", nick=nick)
        self._allow(sid, "plan_approved")

    def approved_version(self, sid: str) -> int:
        """apply가 따르는 버전: 최신 버전이고 승인 상태여야 한다."""
        v = self._s.latest_version(sid)
        if v is None or self._s.plan_meta(sid, v)["status"] != "approved":
            raise self._deny(sid, "no_approved_plan")
        return v

    def start_apply(self, sid: str, nick: str) -> int:
        self._member(sid, nick)
        state = self._s.session(sid)["state"]
        if state not in ("plan_approved", "paused"):
            raise self._deny(sid, f"cannot_start_apply:{state}", nick)
        v = self.approved_version(sid)
        self._s.set_session_state(sid, "applying", nick=nick)
        self._allow(sid, "apply_started")
        return v

    # ---------------- requires 조치 ----------------
    def provide_reason(self, sid: str, nick: str, version: int, step_id: str, text: str) -> None:
        """open_cidr_confirm의 사유. 요청자가 입력하고(입력 가드·마스킹 통과), 승인자가 별도로 확인한다."""
        self._member(sid, nick)
        sess, plan, _ = self._current(sid, version)
        _, step = self._find(plan, step_id)
        if "open_cidr_confirm" not in step.requires:
            raise self._deny(sid, "reason_not_required", nick)
        if nick.lower() != sess["requester"].lower():
            raise self._deny(sid, "reason_by_requester_only", nick)
        v = check_user_input(text, self._masker_for(sid))
        if not v.allow or v.masked_text is None:
            raise self._deny(sid, "reason_blocked:" + ",".join(v.reasons[:2]), nick)
        if len(v.masked_text.strip()) < MIN_REASON:
            raise self._deny(sid, "reason_too_short", nick)
        self._s.revoke_approvals(sid, version, "confirm", f"{step_id}:open_cidr_reason", nick=nick)      # 마지막 사유만 유효
        self._s.add_approval(sid, version, "confirm", f"{step_id}:open_cidr_reason", nick, self._s.plan_meta(sid, version)["plan_hash"], v.masked_text)
        self._allow(sid, "reason_recorded")

    def confirm_requirement(self, sid: str, nick: str, version: int, step_id: str, requirement: str) -> None:
        """승인자(요청자가 아닌 사람)가 `requires` 항목을 확인한다."""
        self._member(sid, nick)
        sess, plan, meta = self._current(sid, version)
        _, step = self._find(plan, step_id)
        if requirement not in step.requires:
            raise self._deny(sid, "requirement_not_on_step", nick)
        if requirement not in CONFIRMER_REQUIRED and requirement != "open_cidr_confirm":
            raise self._deny(sid, f"requirement_not_confirmable:{requirement}", nick)       # dry_run_first·fallback_approval은 확인으로 대신할 수 없다
        if nick.lower() == sess["requester"].lower():
            raise self._deny(sid, "requester_cannot_confirm", nick)
        if requirement == "open_cidr_confirm" and not self._s.approvals(sid, version, "confirm", f"{step_id}:open_cidr_reason"):
            raise self._deny(sid, "reason_missing", nick)
        self._s.add_approval(sid, version, "confirm", f"{step_id}:{requirement}", nick, meta["plan_hash"])
        self._allow(sid, f"confirmed:{requirement}")

    def pending_requirements(self, sid: str, version: int, step_id: str) -> list[str]:
        """화면에 보일 아직 충족되지 않은 `requires` 항목."""
        sess, plan, _ = self._current(sid, version)
        _, step = self._find(plan, step_id)
        return self._missing(sid, version, plan, step, self._s.step_states(sid, version), sess["requester"])

    def _missing(self, sid: str, version: int, plan: Plan, step: Step, step_state: dict[str, dict], requester: str) -> list[str]:
        missing = []
        for r in step.requires:
            if r == "fallback_approval":
                continue                                                       # step 승인이 다른 사람이어야 하는 규칙(needs_other_approval)으로 충족
            if r == "dry_run_first":
                if not self._dry_run_done(plan, step, step_state):
                    missing.append(r)
                continue
            confirms = [a for a in self._s.approvals(sid, version, "confirm", f"{step.id}:{r}") if a["nick"].lower() != requester.lower()]
            if not confirms or (r == "open_cidr_confirm" and not self._s.approvals(sid, version, "confirm", f"{step.id}:open_cidr_reason")):
                missing.append(r)
        return missing

    @staticmethod
    def _dry_run_done(plan: Plan, step: Step, step_state: dict[str, dict]) -> bool:
        """같은 명령의 dry-run step이 앞서 있고 끝났는가."""
        base = _base_tokens(step.command or "")
        if not base:
            return False
        for s in plan.all_steps():
            if s.id == step.id:
                return False
            if s.command and _has_dry_run(s.command) and _base_tokens(s.command) == base and step_state.get(s.id, {}).get("state") == "done":
                return True
        return False

    # ---------------- 게이트 ----------------
    def approve_gate(self, sid: str, nick: str, version: int, phase_id: str) -> None:
        """게이트 phase에 들어가기 전에 요청자가 아닌 사람이 앞 phase 결과를 보고 진행을 확인한다."""
        self._member(sid, nick)
        sess, plan, meta = self._current(sid, version)
        if sess["state"] != "applying":
            raise self._deny(sid, f"not_applying:{sess['state']}", nick)
        idx = next((i for i, p in enumerate(plan.phases) if p.id == phase_id), None)
        if idx is None:
            raise self._deny(sid, "unknown_phase", nick)
        if not plan.phases[idx].gate:
            raise self._deny(sid, "phase_has_no_gate", nick)
        if nick.lower() == sess["requester"].lower():
            raise self._deny(sid, "requester_cannot_approve_gate", nick)
        st = self._s.step_states(sid, version)
        if not all(st[s.id]["state"] in states.SATISFIED_STEP for p in plan.phases[:idx] for s in p.steps):
            raise self._deny(sid, "previous_phase_incomplete", nick)
        self._s.add_approval(sid, version, "gate", phase_id, nick, meta["plan_hash"])
        self._allow(sid, "gate_approved")

    # ---------------- step 승인 ----------------
    def approve_step(self, sid: str, nick: str, version: int, step_id: str, plan_hash_seen: str) -> StepGrant | None:
        """step을 승인한다. 서버가 실행하는 step은 `approved`로, manual·iac는 `awaiting_report`로 간다.
        실행 가능한 step이면 `StepGrant`를 돌려준다(manual·iac는 None)."""
        self._member(sid, nick)
        sess, plan, meta = self._current(sid, version)
        if sess["state"] != "applying":
            raise self._deny(sid, f"not_applying:{sess['state']}", nick)
        if version != self.approved_version(sid) or plan_hash_seen != meta["plan_hash"]:
            raise self._deny(sid, "plan_hash_mismatch", nick)
        pi, step = self._find(plan, step_id)
        st = self._s.step_states(sid, version)
        if st[step_id]["state"] != "pending":
            raise self._deny(sid, f"step_not_pending:{st[step_id]['state']}", nick)
        manual_like = step.op in ("manual", "none")                    # 서버가 실행하지 않는 step(manual, iac, 수동 체크): 사람이 보고한다
        if step.needs_other_approval and nick.lower() == sess["requester"].lower():
            raise self._deny(sid, "requester_cannot_approve_change", nick)
        if not all(st[s.id]["state"] in states.SATISFIED_STEP for p in plan.phases[:pi] for s in p.steps):
            raise self._deny(sid, "previous_phase_incomplete", nick)
        for dep in (*step.depends_on, *([step.check_for] if step.check_for else [])):
            if st[dep]["state"] not in states.SATISFIED_STEP:
                raise self._deny(sid, f"dependency_not_done:{dep}", nick)
        if plan.phases[pi].gate and not self._s.approvals(sid, version, "gate", plan.phases[pi].id):
            raise self._deny(sid, "gate_not_approved", nick)
        missing = self._missing(sid, version, plan, step, st, sess["requester"])
        if missing:
            raise self._deny(sid, "missing_requirement:" + ",".join(missing), nick)
        self._s.add_approval(sid, version, "step", step_id, nick, meta["plan_hash"])
        self._s.transition_step(sid, version, step_id, "approved", expect="pending", nick=nick)
        if manual_like:
            self._s.transition_step(sid, version, step_id, "awaiting_report", expect="approved", nick=nick)
        self._allow(sid, "step_approved")
        return None if manual_like else self.grant_for(sid, version, step_id)

    def auto_approve_check(self, sid: str, version: int, step_id: str) -> None:
        """변경 step이 끝나면 서버가 그 확인(읽기) step을 자동 승인한다. 읽기 확인 step만, 확인 대상이 끝났을 때만 가능하다."""
        sess, plan, meta = self._current(sid, version)
        if sess["state"] != "applying":
            raise self._deny(sid, f"not_applying:{sess['state']}")
        _, step = self._find(plan, step_id)
        st = self._s.step_states(sid, version)
        if step.op != "read" or not step.check_for or step.needs_other_approval or step.requires:
            raise self._deny(sid, "not_auto_approvable")
        if st[step.check_for]["state"] != "done" or st[step_id]["state"] != "pending":
            raise self._deny(sid, "auto_approve_preconditions")
        pi, _ = self._find(plan, step_id)
        if not all(st[s.id]["state"] in states.SATISFIED_STEP for p in plan.phases[:pi] for s in p.steps):
            raise self._deny(sid, "previous_phase_incomplete")                  # 자동 승인도 사람의 승인과 같은 순서 조건을 지킨다
        for dep in step.depends_on:
            if st[dep]["state"] not in states.SATISFIED_STEP:
                raise self._deny(sid, f"dependency_not_done:{dep}")
        if plan.phases[pi].gate and not self._s.approvals(sid, version, "gate", plan.phases[pi].id):
            raise self._deny(sid, "gate_not_approved")
        self._s.add_approval(sid, version, "step", step_id, AUTO, meta["plan_hash"])
        self._s.transition_step(sid, version, step_id, "approved", expect="pending", nick=AUTO)
        self._allow(sid, "check_auto_approved")

    def grant_for(self, sid: str, version: int, step_id: str) -> StepGrant:
        """저장된 승인 기록에서 승인 자격을 다시 계산한다. 실행 직전마다 호출한다."""
        sess, plan, meta = self._current(sid, version)
        if self._s.plan_meta(sid, version)["status"] != "approved":
            raise self._deny(sid, "plan_not_approved")
        _, step = self._find(plan, step_id)
        requester = sess["requester"].lower()
        rows = [a for a in self._s.approvals(sid, version, "step", step_id) if a["plan_hash"] == meta["plan_hash"]]
        if not rows:
            raise self._deny(sid, "step_not_approved")
        others = tuple(a["nick"] for a in rows if a["nick"].lower() != requester)
        if step.needs_other_approval and not others:
            raise self._deny(sid, "four_eyes_approval_required")
        st = self._s.step_states(sid, version)
        missing = self._missing(sid, version, plan, step, st, sess["requester"])
        if missing:
            raise self._deny(sid, "missing_requirement:" + ",".join(missing))
        confirmed = frozenset(r for r in step.requires if r not in missing)
        return StepGrant(step_id, meta["plan_hash"], bool(others), confirmed, tuple(a["nick"] for a in rows))

    def reset_step_approvals(self, sid: str, version: int, step_id: str, *, nick: str | None = None) -> None:
        """재시도할 때 그 step의 승인·확인을 무효로 하고 다시 받게 한다."""
        self._s.revoke_approvals(sid, version, "step", step_id, nick=nick)
        for r in ("open_cidr_confirm", "open_cidr_reason", *CONFIRMER_REQUIRED):
            self._s.revoke_approvals(sid, version, "confirm", f"{step_id}:{r}", nick=nick)
