"""계획(plan → phase → step) 스키마, 서버 보강, 구조 검증, 해시.

모델은 `PlanDraft`만 만든다. 읽기/변경 분류, 위험, 근거, 폴백 여부, 영향(`impact`), 게이트 잠금은 모델이 정하지 않는다.
서버가 `build_plan`에서 `CommandGuard.check`의 판정으로 채운다(분류는 허용 목록 규칙이고 모델 판단 금지, 애매하면 변경).
`Plan`은 승인·저장·UI·실행의 기준 형태이고, `plan_hash`는 승인 잠금에 쓴다("승인된 내용 = 실행된 내용").

구조 규칙(위반하면 `PlanInvalid`, 코드 목록은 `PlanInvalid.codes`):
- step id는 plan 안에서 유일하고, `depends_on`·`check_for`는 앞에 나온 step만 가리킨다(순서가 곧 위상 정렬).
- 명령 가드가 막은 명령, 모델이 붙인 종류와 가드 판정이 다른 step(예: `cli`인데 manual 명령)은 계획에 넣을 수 없다.
- 변경 step마다 같은 phase 안에서 뒤에 오는 확인(`check_for`) step이 있어야 한다.
- 되돌릴 수 없는(`irreversible`) step을 포함한 phase의 게이트는 서버가 켜고 잠근다(제거 불가).
- plan·phase당 step 수 상한(`agent_limits.check_plan_size`).
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.guards.agent_limits import MAX_PHASE_STEPS, PlanTooLarge, check_plan_size
from app.guards.boundary import leak_kinds
from app.guards.command_guard import CommandGuard
from app.guards.masking import Masker

STEP_ID = r"^[a-z][a-z0-9-]{0,30}$"
StepKind = Literal["cli", "manual", "iac", "check", "poll"]


class PlanInvalid(ValueError):
    def __init__(self, codes: list[str]):
        super().__init__(", ".join(codes[:8]))
        self.codes = codes


# ---------------- 모델이 만드는 초안 ----------------
class PollSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    interval_seconds: int = Field(ge=5, le=300)
    max_seconds: int = Field(ge=10, le=7200)
    until: str = Field(max_length=120)            # 완료 조건(사람이 읽는 문장)


class StepDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=STEP_ID)
    kind: StepKind
    title: str = Field(min_length=1, max_length=120)
    command: str | None = Field(default=None, max_length=600)     # 마스킹된 명령. cli·manual·poll은 필수
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    check_for: str | None = Field(default=None, pattern=STEP_ID)  # 이 step이 확인하는 변경 step의 id
    poll: PollSpec | None = None
    checklist: list[str] = Field(default_factory=list, max_length=12)   # iac·check(수동 체크)용
    rationale: str = Field(default="", max_length=200)


class PhaseDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=STEP_ID)
    title: str = Field(min_length=1, max_length=100)
    purpose: str = Field(default="", max_length=200)
    preconditions: list[str] = Field(default_factory=list, max_length=8)
    steps: list[StepDraft] = Field(min_length=1, max_length=MAX_PHASE_STEPS)
    gate: bool = False                                # 모델이 제안하는 게이트. irreversible이면 서버가 강제로 켠다
    gate_reason: str = Field(default="", max_length=160)
    rollback: str = Field(default="", max_length=300)


class PlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=300)
    phases: list[PhaseDraft] = Field(min_length=1, max_length=20)


# ---------------- 서버가 보강한 계획 ----------------
class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    kind: StepKind
    title: str
    command: str | None = None
    depends_on: tuple[str, ...] = ()
    check_for: str | None = None
    poll: PollSpec | None = None
    checklist: tuple[str, ...] = ()
    rationale: str = ""
    # --- 서버가 채우는 값(가드 판정) ---
    op: Literal["read", "mutate", "manual", "none"] = "none"     # none: 명령이 없는 step(iac·check)
    risk: str = "low"
    grounded: bool = True
    chunk_id: str | None = None
    flags: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    impact: dict = Field(default_factory=dict)

    @property
    def fallback(self) -> bool:
        return self.op in ("read", "mutate") and not self.grounded

    @property
    def needs_other_approval(self) -> bool:
        """변경, 폴백(AI 추정)은 요청자가 아닌 사람의 승인이 필요하다."""
        return self.op == "mutate" or self.fallback

    @property
    def irreversible(self) -> bool:
        return self.impact.get("reversible") == "irreversible"


class Phase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    title: str
    purpose: str = ""
    preconditions: tuple[str, ...] = ()
    steps: tuple[Step, ...]
    gate: bool = False
    gate_locked: bool = False                         # True면 게이트를 제거할 수 없다
    gate_reason: str = ""
    rollback: str = ""


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    summary: str
    phases: tuple[Phase, ...]
    iac_managed: bool = False

    def all_steps(self) -> list[Step]:
        return [s for p in self.phases for s in p.steps]


# ---------------- 구성과 검증 ----------------
def build_plan(draft: PlanDraft, guard: CommandGuard, masker: Masker, *, iac_managed: bool = False) -> Plan:
    codes: list[str] = []
    text = " ".join([draft.summary] + [
        " ".join([p.title, p.purpose, p.rollback, p.gate_reason, *p.preconditions] + [
            " ".join([s.title, s.rationale, s.command or "", *s.checklist, s.poll.until if s.poll else ""]) for s in p.steps])
        for p in draft.phases])
    if leak_kinds(text, masker):                        # 모델 출력에 실제 값이 있으면 계획으로 쓰지 않는다
        raise PlanInvalid(["model_output_unsafe"])
    try:
        check_plan_size([len(p.steps) for p in draft.phases])
    except PlanTooLarge:
        raise PlanInvalid(["plan_too_large"]) from None

    seen: dict[str, Step] = {}
    phases: list[Phase] = []
    for pd in draft.phases:
        steps: list[Step] = []
        for sd in pd.steps:
            step = _enrich(sd, guard, masker, codes)
            if sd.id in seen:
                codes.append(f"duplicate_step_id:{sd.id}")
            for dep in (*sd.depends_on, *([sd.check_for] if sd.check_for else [])):
                if dep not in seen:
                    codes.append(f"unknown_or_forward_reference:{sd.id}->{dep}")
            if sd.check_for and sd.check_for in seen and seen[sd.check_for].op != "mutate":
                codes.append(f"check_target_not_mutate:{sd.id}")
            seen[sd.id] = step
            steps.append(step)
        _require_checks(steps, codes)
        locked = any(s.irreversible for s in steps)
        phases.append(Phase(
            id=pd.id, title=pd.title, purpose=pd.purpose, preconditions=tuple(pd.preconditions), steps=tuple(steps),
            gate=pd.gate or locked, gate_locked=locked, gate_reason=pd.gate_reason or ("되돌릴 수 없는 step 포함" if locked else ""),
            rollback=pd.rollback,
        ))
    if codes:
        raise PlanInvalid(codes)
    return Plan(summary=draft.summary, phases=tuple(phases), iac_managed=iac_managed)


def _enrich(sd: StepDraft, guard: CommandGuard, masker: Masker, codes: list[str]) -> Step:
    base = dict(id=sd.id, kind=sd.kind, title=sd.title, command=sd.command, depends_on=tuple(sd.depends_on),
                check_for=sd.check_for, poll=sd.poll, checklist=tuple(sd.checklist), rationale=sd.rationale)
    needs_command = sd.kind in ("cli", "manual", "poll")
    if needs_command and not sd.command:
        codes.append(f"command_required:{sd.id}")
        return Step(**base)
    if sd.kind == "poll" and sd.poll is None:
        codes.append(f"poll_spec_required:{sd.id}")
    if sd.kind != "poll" and sd.poll is not None:
        codes.append(f"poll_spec_unexpected:{sd.id}")
    if sd.kind in ("iac", "check") and sd.command is None:
        return Step(**base)                              # 코드 변경 체크리스트·수동 체크: 실행하지 않는다
    v = guard.check(sd.command, masker)
    if not v.allow:
        codes.append(f"blocked_command:{sd.id}")
        return Step(**base)
    if sd.kind == "manual" and v.kind != "manual":
        codes.append(f"kind_mismatch:{sd.id}")
    elif sd.kind in ("cli", "poll") and v.kind == "manual":
        codes.append(f"kind_mismatch:{sd.id}")
    if sd.kind == "poll" and v.kind != "read":
        codes.append(f"poll_requires_read:{sd.id}")
    if sd.kind == "check" and v.kind != "read":
        codes.append(f"check_requires_read:{sd.id}")
    if sd.check_for and v.kind != "read":
        codes.append(f"check_requires_read:{sd.id}")
    return Step(**base, op=v.kind, risk=v.risk, grounded=v.grounded, chunk_id=v.chunk_id, flags=v.flags, requires=v.requires, impact=dict(v.impact))


def _require_checks(steps: list[Step], codes: list[str]) -> None:
    """변경 step마다 같은 phase 안에서 그 뒤에 오는 확인 step이 있어야 한다."""
    for i, s in enumerate(steps):
        if s.op == "mutate" and not any(t.check_for == s.id for t in steps[i + 1:]):
            codes.append(f"missing_check:{s.id}")


# ---------------- 해시 ----------------
def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=list)


def step_hash(step: Step) -> str:
    return hashlib.sha256(_canon(step.model_dump(mode="json")).encode("utf-8")).hexdigest()


def plan_hash(plan: Plan) -> str:
    """승인 잠금용 해시. 사용자에게 보이는 모든 내용(제목, 게이트, 롤백 포함)과 step 해시 순서를 묶는다."""
    body = {
        "summary": plan.summary, "iac_managed": plan.iac_managed,
        "phases": [
            {"id": p.id, "title": p.title, "purpose": p.purpose, "preconditions": list(p.preconditions), "gate": p.gate,
             "gate_locked": p.gate_locked, "gate_reason": p.gate_reason, "rollback": p.rollback,
             "steps": [step_hash(s) for s in p.steps]}
            for p in plan.phases
        ],
    }
    return hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()
