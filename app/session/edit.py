"""plan 검토 중의 직접 수정: 커스텀 step 추가·수정·삭제·순서 변경, 게이트 설정.

수정은 항상 `build_plan`(서버 검증)으로 다시 만든 새 `Plan`을 돌려준다. 사용자가 입력한 문장은 입력 가드와 마스킹을 거치고,
명령은 `CommandGuard`가 검사하며(차단·폴백 표시·위험 판정은 서버가 다시 한다), 변경 step의 확인 step 짝, 의존 순서,
게이트 잠금(되돌릴 수 없는 step이 있는 phase)도 `build_plan`이 다시 강제한다. 결과는 새 plan 버전으로 올려 다시 승인받는다.
서버가 넣은 step(`auto`)은 수정해도 표시가 유지된다.
"""
from __future__ import annotations

from typing import Any, Callable

from app.guards.command_guard import CommandGuard
from app.guards.input_guard import check_user_input
from app.guards.masking import Masker
from app.planner.schema import Plan, PlanDraft, PlanInvalid, PhaseDraft, StepDraft, build_plan

TEXT_FIELDS = ("title", "command", "rationale")
EDITABLE = frozenset({"title", "command", "rationale", "kind", "depends_on", "check_for", "poll", "checklist", "irreversible"})


class PlanEditor:
    def __init__(self, guard: CommandGuard, masker_for: Callable[[str], Masker]):
        self._guard, self._masker_for = guard, masker_for

    # ---------------- 공개 ----------------
    def add_step(self, sid: str, plan: Plan, phase_id: str, step: dict[str, Any], *, after: str | None = None) -> Plan:
        """phase의 `after` step 뒤(없으면 맨 뒤)에 커스텀 step을 넣는다."""
        return self.add_steps(sid, plan, phase_id, [step], after=after)

    def add_steps(self, sid: str, plan: Plan, phase_id: str, steps: list[dict[str, Any]], *, after: str | None = None) -> Plan:
        """여러 step을 한 번에 넣는다. 변경 step과 그 확인 step처럼 짝으로 있어야 하는 step은 함께 넣어야 검증을 통과한다."""
        masker = self._masker_for(sid)
        drafts = [self._clean(sid, StepDraft.model_validate(s)) for s in steps]
        phases = self._drafts(plan)
        ph = self._phase(phases, phase_id)
        existing = {s.id for p in phases for s in p.steps}
        for d in drafts:
            if d.id in existing:
                raise PlanInvalid([f"duplicate_step_id:{d.id}"])
            existing.add(d.id)
        idx = len(ph.steps) if after is None else self._index(ph, after) + 1
        ph.steps[idx:idx] = drafts
        return self._rebuild(plan, phases, masker)

    def update_step(self, sid: str, plan: Plan, step_id: str, **fields: Any) -> Plan:
        bad = set(fields) - EDITABLE
        if bad:
            raise PlanInvalid([f"field_not_editable:{sorted(bad)[0]}"])
        masker = self._masker_for(sid)
        phases = self._drafts(plan)
        ph, i = self._locate(phases, step_id)
        cur = ph.steps[i].model_dump()
        cur.update({k: (list(v) if k in ("depends_on", "checklist") and v is not None else v) for k, v in fields.items()})
        ph.steps[i] = self._clean(sid, StepDraft.model_validate(cur))
        return self._rebuild(plan, phases, masker)

    def delete_step(self, sid: str, plan: Plan, step_id: str) -> Plan:
        """step을 지운다. 다른 step이 의존하거나 확인 대상으로 삼으면, 변경 step의 확인 step이면 `build_plan`이 거부한다."""
        phases = self._drafts(plan)
        ph, i = self._locate(phases, step_id)
        del ph.steps[i]
        if not ph.steps:
            phases.remove(ph)
            if not phases:
                raise PlanInvalid(["plan_empty"])
        return self._rebuild(plan, phases, self._masker_for(sid))

    def move_step(self, sid: str, plan: Plan, step_id: str, *, after: str | None) -> Plan:
        """같은 phase 안에서 step을 `after` 뒤로 옮긴다(None이면 phase 맨 앞). 의존 순서가 깨지면 `build_plan`이 거부한다."""
        phases = self._drafts(plan)
        ph, i = self._locate(phases, step_id)
        step = ph.steps.pop(i)
        j = 0 if after is None else self._index(ph, after) + 1
        ph.steps.insert(j, step)
        return self._rebuild(plan, phases, self._masker_for(sid))

    def set_gate(self, sid: str, plan: Plan, phase_id: str, gate: bool) -> Plan:
        """게이트를 켜거나 끈다. 되돌릴 수 없는 step이 있는 phase의 게이트는 끌 수 없다(`build_plan`이 다시 켠다)."""
        phases = self._drafts(plan)
        self._phase(phases, phase_id).gate = gate
        return self._rebuild(plan, phases, self._masker_for(sid))

    # ---------------- 내부 ----------------
    def _clean(self, sid: str, d: StepDraft) -> StepDraft:
        """사용자가 입력한 문장을 입력 가드와 마스킹에 통과시킨다(실제 값은 플레이스홀더로 바뀌고, 시크릿·인젝션이면 거부)."""
        masker = self._masker_for(sid)
        data = d.model_dump()
        for f in TEXT_FIELDS:
            if data.get(f):
                v = check_user_input(data[f], masker)
                if not v.allow or v.masked_text is None:
                    raise PlanInvalid([f"input_blocked:{f}:{','.join(v.reasons[:2])}"])
                data[f] = v.masked_text
        data["checklist"] = []
        for item in d.checklist:
            v = check_user_input(item, masker)
            if not v.allow or v.masked_text is None:
                raise PlanInvalid(["input_blocked:checklist:" + ",".join(v.reasons[:2])])
            data["checklist"].append(v.masked_text)
        if d.poll is not None:
            v = check_user_input(d.poll.until, masker)
            if not v.allow or v.masked_text is None:
                raise PlanInvalid(["input_blocked:poll:" + ",".join(v.reasons[:2])])
            data["poll"] = {**d.poll.model_dump(), "until": v.masked_text}
        return StepDraft.model_validate(data)

    @staticmethod
    def _drafts(plan: Plan) -> list[PhaseDraft]:
        out = []
        for p in plan.phases:
            steps = [StepDraft(id=s.id, kind=s.kind, title=s.title, command=s.command, depends_on=list(s.depends_on), check_for=s.check_for,
                               poll=s.poll, checklist=list(s.checklist), rationale=s.rationale, irreversible=s.declared_irreversible)
                     for s in p.steps]
            out.append(PhaseDraft(id=p.id, title=p.title, purpose=p.purpose, preconditions=list(p.preconditions), steps=steps,
                                  gate=p.gate, gate_reason=p.gate_reason, rollback=p.rollback))
        return out

    def _rebuild(self, old: Plan, phases: list[PhaseDraft], masker: Masker) -> Plan:
        new = build_plan(PlanDraft(summary=old.summary, phases=phases), self._guard, masker, iac_managed=old.iac_managed)
        auto_ids = {s.id for s in old.all_steps() if s.auto}
        if not auto_ids & {s.id for s in new.all_steps()}:
            return new
        fixed = tuple(p.model_copy(update={"steps": tuple(s.model_copy(update={"auto": True}) if s.id in auto_ids else s for s in p.steps)})
                      for p in new.phases)
        return new.model_copy(update={"phases": fixed})

    @staticmethod
    def _phase(phases: list[PhaseDraft], phase_id: str) -> PhaseDraft:
        for p in phases:
            if p.id == phase_id:
                return p
        raise PlanInvalid([f"unknown_phase:{phase_id}"])

    @staticmethod
    def _locate(phases: list[PhaseDraft], step_id: str) -> tuple[PhaseDraft, int]:
        for p in phases:
            for i, s in enumerate(p.steps):
                if s.id == step_id:
                    return p, i
        raise PlanInvalid([f"unknown_step:{step_id}"])

    @staticmethod
    def _index(ph: PhaseDraft, step_id: str) -> int:
        for i, s in enumerate(ph.steps):
            if s.id == step_id:
                return i
        raise PlanInvalid([f"unknown_step_in_phase:{step_id}"])
