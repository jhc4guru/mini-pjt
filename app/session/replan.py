"""부분 재계획: apply를 일시정지하고 남은 step만 새 plan 버전으로 고쳐 다시 승인받은 뒤 재개한다.

- apply 중에는 plan을 고칠 수 없다. 일시정지(`paused`) 상태에서만 새 버전을 올릴 수 있다.
- 완료·건너뜀 step은 잠긴다. 새 버전에 같은 id, 같은 내용(step 해시), 같은 phase, 같은 상대 순서로 있어야 하고, 그 상태를 이어받는다
  (완료 step 해시 승계). 나머지 step은 모두 `pending`으로 시작하고 이전 승인은 효력이 없다.
- 새 버전은 plan 일괄 승인(요청자가 아닌 사람)을 다시 받아야 하고, 이미 지난 게이트의 승인은 새 해시로 이어받는다.
- 모든 변경은 `ApprovalEngine.submit_plan`을 거친다(상태 전이·이벤트·감사 기록).
"""
from __future__ import annotations

from app.planner.schema import Plan, step_hash
from app.session import states
from app.session.approval import ApprovalEngine, ApprovalError
from app.session.store import SessionStore


class ReplanEngine:
    def __init__(self, store: SessionStore, approvals: ApprovalEngine):
        self._s, self._ap = store, approvals

    def pause(self, sid: str, nick: str) -> None:
        """참여자가 apply를 일시정지한다. 실행 중인 서버 step이 있으면 끝난 뒤에 한다(poll 조회는 멈출 수 있다)."""
        if self._s.role_of(sid, nick) is None:
            raise self._ap._deny(sid, "not_a_participant", nick)
        if self._s.session(sid)["state"] != "applying":
            raise self._ap._deny(sid, "pause_needs_applying", nick)
        v = self._s.latest_version(sid)
        plan = self._s.load_plan(sid, v)
        kinds = {s.id: s.kind for s in plan.all_steps()}
        for step_id, st in self._s.step_states(sid, v).items():
            if st["state"] == "running" and kinds[step_id] != "poll":
                raise self._ap._deny(sid, f"step_running:{step_id}", nick)
        self._s.set_session_state(sid, "paused", nick=nick)

    def submit_replan(self, sid: str, nick: str, new_plan: Plan) -> int:
        """남은 step을 고친 새 plan을 올린다. 완료·건너뜀 step이 바뀌었으면 거부한다."""
        if self._s.role_of(sid, nick) is None:
            raise self._ap._deny(sid, "not_a_participant", nick)
        if self._s.session(sid)["state"] != "paused":
            raise self._ap._deny(sid, "replan_needs_paused", nick)
        old_v = self._s.latest_version(sid)
        old = self._s.load_plan(sid, old_v)
        st = self._s.step_states(sid, old_v)
        locked = {sid_: v["state"] for sid_, v in st.items() if v["state"] in states.SATISFIED_STEP}
        code = self._locked_violation(old, new_plan, set(locked))
        if code:
            raise self._ap._deny(sid, code, nick)
        v = self._ap.submit_plan(sid, nick, new_plan, parent_version=old_v, carry=locked)
        done_phases = {p.id for p in old.phases if any(s.id in locked for s in p.steps)}
        new_hash = self._s.plan_meta(sid, v)["plan_hash"]
        for a in self._s.approvals(sid, old_v, "gate"):                  # 이미 지난 게이트는 새 해시로 이어받는다
            if a["target"] in done_phases and any(p.id == a["target"] and p.gate for p in new_plan.phases):
                self._s.add_approval(sid, v, "gate", a["target"], a["nick"], new_hash, "carried")
        self._s.log(sid, "replan", f"v{old_v}->v{v} carried={len(locked)}", nick=nick)
        return v

    @staticmethod
    def _locked_violation(old: Plan, new: Plan, locked: set[str]) -> str | None:
        old_steps = {s.id: (p.id, s) for p in old.phases for s in p.steps}
        new_steps = {s.id: (p.id, s) for p in new.phases for s in p.steps}
        for sid_ in locked:
            if sid_ not in new_steps:
                return f"locked_step_removed:{sid_}"
            (op, os_), (np_, ns) = old_steps[sid_], new_steps[sid_]
            if op != np_:
                return f"locked_step_moved:{sid_}"
            if step_hash(os_) != step_hash(ns):
                return f"locked_step_changed:{sid_}"
        order_old = [s.id for p in old.phases for s in p.steps if s.id in locked]
        order_new = [s.id for p in new.phases for s in p.steps if s.id in locked]
        if order_old != order_new:
            return "locked_step_reordered"
        return None
