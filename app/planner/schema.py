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

`build_plan`은 마지막에 `postprocess.apply_postprocess`로 사전 점검 읽기 step과 IaC 동기화 `iac` step을 서버가 더하고 경고를 남긴다(`Step.auto`, `Plan.warnings`).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.guards.agent_limits import MAX_PHASE_STEPS, PlanTooLarge, check_plan_size
from app.guards.refs import find_refs, output_not_json
from app.guards.boundary import leak_kinds
from app.guards.command_guard import HIGH_FLAGS, CommandGuard
from app.guards.masking import Masker

STEP_ID = r"^[a-z][a-z0-9-]{0,39}$"
StepKind = Literal["cli", "manual", "iac", "check", "poll"]


class PlanInvalid(ValueError):
    def __init__(self, codes: list[str], detail: tuple[str, ...] = ()):
        super().__init__(", ".join(codes[:8]))
        self.codes = codes
        self.detail = detail            # 진단용 부가 정보(누출 종류 이름 등, 값 없음)


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
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    check_for: str | None = Field(default=None, pattern=STEP_ID)  # 이 step이 확인하는 변경 step의 id
    poll: PollSpec | None = None
    checklist: list[str] = Field(default_factory=list, max_length=12)   # iac·check(수동 체크)용
    rationale: str = Field(default="", max_length=200)
    irreversible: bool = False                       # 되돌릴 수 없는 변경이면 true(iac·manual 등 문서 impact가 없는 step용). 서버는 더 엄격한 쪽으로만 반영한다


# 문서 근거가 없는 iac·manual step은 되돌릴 수 없는지를 모델이 표시한다. 표시가 빠지기 쉬운 대표 작업(컨트롤 플레인·클러스터 버전 변경, 클러스터 삭제)은
# 서버가 알아보고 표시가 없으면 검증 실패로 돌려 고치게 한다(D-64, 더 엄격한 방향이다. 키워드는 좁게 둔다).
_IRREVERSIBLE_HINT = re.compile(
    r"kubernetes_version|terraform\s+destroy|클러스터\s*삭제|delete[-_\s]*cluster"
    r"|(?:컨트롤\s*플레인|control[-_\s]*plane|클러스터\s*버전|cluster[-_\s]*version).{0,24}(?:업그레이드|버전\s*(?:변경|상향|올)|upgrade|version)",
    re.I)


def needs_irreversible_mark(kind: str, title: str, rationale: str, checklist, irreversible: bool) -> bool:
    """iac·manual step이 되돌릴 수 없는 작업으로 보이는데 `irreversible` 표시가 없으면 True."""
    if kind not in ("iac", "manual") or irreversible:
        return False
    return bool(_IRREVERSIBLE_HINT.search(" ".join([title or "", rationale or "", *(checklist or [])])))


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
    auto: bool = False                                              # 서버가 후처리에서 넣은 step(사전 점검, 코드 동기화 확인)
    declared_irreversible: bool = False                             # 모델이 표시한 되돌릴 수 없음(문서 impact가 없는 step). 게이트 잠금만 강화한다

    @property
    def fallback(self) -> bool:
        return self.op in ("read", "mutate") and not self.grounded

    @property
    def needs_other_approval(self) -> bool:
        """변경, 폴백(AI 추정)은 요청자가 아닌 사람의 승인이 필요하다."""
        return self.op == "mutate" or self.fallback

    @property
    def preapprove_blockers(self) -> tuple[str, ...]:
        """plan 확정(일괄 승인) 때 절차별 승인을 함께 받을 수 없는 이유(D-50). 비어 있으면 **저위험 변경 절차**라서 함께 사전 승인된다.
        서버가 실행하는 변경 중 문서 근거가 있고, 위험이 high가 아니며, 중단·교체·되돌릴 수 없음이 없고, 추가 조치·강한 경고 표시가 없는 것이다.
        IaC 코드와 어긋날 수 있다(`iac_drift`) 같은 안내는 위험 요인이 아니다."""
        if self.op != "mutate" or self.kind not in ("cli",) or not self.command:
            return ("not_server_change",)
        out: list[str] = []
        if not self.grounded:
            out.append("not_grounded")
        if self.risk not in ("low", "medium"):
            out.append("high_risk")
        if self.irreversible:
            out.append("irreversible")
        disruption = self.impact.get("disruption")
        if disruption != "none":
            out.append("disruption_unknown" if disruption is None else f"disruption_{disruption}")
        if self.impact.get("replaces"):
            out.append("replaces")
        if self.requires:
            out.append("needs_confirmation")
        out += [f"flag:{f}" for f in self.flags if f in HIGH_FLAGS or f == "scale_to_zero"]
        return tuple(dict.fromkeys(out))

    @property
    def preapprovable(self) -> bool:
        return not self.preapprove_blockers

    @property
    def irreversible(self) -> bool:
        """문서의 impact 또는 모델의 표시. 어느 쪽이든 true면 그 phase의 게이트가 잠긴다(모델이 false로 해도 문서 값은 바뀌지 않는다)."""
        return self.impact.get("reversible") == "irreversible" or self.declared_irreversible


class Phase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    title: str
    purpose: str = ""
    preconditions: tuple[str, ...] = ()
    steps: tuple[Step, ...]
    gate: bool = False
    gate_locked: bool = False                         # True면 사유 없이는 게이트를 제거할 수 없다(D-64: 사유를 적어 해제하면 False, gate_waiver에 사유가 남는다)
    gate_reason: str = ""
    gate_waiver: str = ""                             # 되돌릴 수 없는 step이 있는 phase의 게이트를 사람이 사유와 함께 해제했을 때의 사유(모델은 쓸 수 없다)
    rollback: str = ""


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    summary: str
    phases: tuple[Phase, ...]
    iac_managed: bool = False
    warnings: tuple[str, ...] = ()                                  # 후처리 경고 코드(검토자에게 표시, 해시에 포함)

    def all_steps(self) -> list[Step]:
        return [s for p in self.phases for s in p.steps]


# ---------------- 구성과 검증 ----------------
def build_plan(draft: PlanDraft, guard: CommandGuard, masker: Masker, *, iac_managed: bool = False, postprocess: bool = True,
               suppressed: frozenset[str] = frozenset(), frozen: frozenset[str] = frozenset(), gate_waivers: dict[str, str] | None = None) -> Plan:
    """`gate_waivers`: phase id → 사람이 적은 사유. 되돌릴 수 없는 step이 있는 phase의 게이트는 사유가 있고 게이트를 껐을 때만 해제된다(D-64, 모델 출력으로는 해제되지 않는다)."""
    codes: list[str] = []
    text = " ".join([draft.summary] + [
        " ".join([p.title, p.purpose, p.rollback, p.gate_reason, *p.preconditions] + [
            " ".join([s.title, s.rationale, s.command or "", *s.checklist, s.poll.until if s.poll else ""]) for s in p.steps])
        for p in draft.phases])
    kinds = leak_kinds(text, masker)
    if kinds:                                           # 모델 출력에 실제 값이 있으면 계획으로 쓰지 않는다(어떤 종류인지는 이름만 남긴다)
        raise PlanInvalid(["model_output_unsafe"], detail=tuple(kinds))
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
            if needs_irreversible_mark(sd.kind, sd.title, sd.rationale, sd.checklist, sd.irreversible):
                codes.append(f"irreversible_not_marked:{sd.id}")
            if sd.id in seen:
                codes.append(f"duplicate_step_id:{sd.id}")
            for dep in (*sd.depends_on, *([sd.check_for] if sd.check_for else [])):
                if dep not in seen:
                    codes.append(f"unknown_or_forward_reference:{sd.id}->{dep}")
            if sd.check_for and sd.check_for in seen and not _is_change(seen[sd.check_for]):
                codes.append(f"check_target_not_change:{sd.id}")
            step = _check_refs(sd, step, seen, codes)
            seen[sd.id] = step
            steps.append(step)
        _require_checks(steps, codes)
        irrev = any(s.irreversible for s in steps)
        waiver = ((gate_waivers or {}).get(pd.id) or "").strip()[:160]
        waived = irrev and not pd.gate and bool(waiver)
        locked = irrev and not waived
        phases.append(Phase(
            id=pd.id, title=pd.title, purpose=pd.purpose, preconditions=tuple(pd.preconditions), steps=tuple(steps),
            gate=pd.gate or locked, gate_locked=locked, gate_reason=pd.gate_reason or ("되돌릴 수 없는 step 포함" if locked else ""),
            gate_waiver=waiver if waived else "", rollback=pd.rollback,
        ))
    if codes:
        raise PlanInvalid(codes)
    plan = Plan(summary=draft.summary, phases=tuple(phases), iac_managed=iac_managed)
    if not postprocess:
        return plan
    from app.planner.postprocess import apply_postprocess      # 순환 import를 피한다
    return apply_postprocess(plan, guard, masker, suppressed, frozen)


def _check_refs(sd: StepDraft, step: Step, seen: dict[str, Step], codes: list[str]) -> Step:
    """앞 절차 결과 참조(D-48): 서버가 실행하는 앞 절차만 가리킬 수 있고, 참조한 절차는 의존으로 자동 추가한다. 사람이 실행하는 절차에는 쓸 수 없다."""
    refs = find_refs(sd.command)
    if not refs:
        return step
    if sd.kind not in ("cli", "poll"):
        codes.append(f"ref_not_allowed_here:{sd.id}")
        return step
    need: list[str] = []
    for rid, _ in refs:
        tgt = seen.get(rid)
        if tgt is None:
            codes.append(f"ref_unknown_or_forward:{sd.id}->{rid}")
        elif tgt.kind not in ("cli", "poll") or tgt.op not in ("read", "mutate") or not tgt.command:
            codes.append(f"ref_target_not_runnable:{sd.id}->{rid}")
        elif output_not_json(tgt.command):
            codes.append(f"ref_target_not_json:{sd.id}->{rid}")
        elif rid not in step.depends_on and rid not in need:
            need.append(rid)
    return step.model_copy(update={"depends_on": (*step.depends_on, *need)}) if need else step


def _is_change(step: Step) -> bool:
    """확인 step이 확인할 수 있는 대상: 서버가 실행하는 변경, 사람이 실행하는 manual, 코드 변경(iac) step."""
    return step.op in ("mutate", "manual") or step.kind == "iac"


def _enrich(sd: StepDraft, guard: CommandGuard, masker: Masker, codes: list[str]) -> Step:
    base = dict(id=sd.id, kind=sd.kind, title=sd.title, command=sd.command, depends_on=tuple(sd.depends_on),
                check_for=sd.check_for, poll=sd.poll, checklist=tuple(sd.checklist), rationale=sd.rationale,
                declared_irreversible=sd.irreversible)
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
        codes.append(f"blocked_command:{sd.id}:{','.join(v.blocks[:3])}")      # 차단 사유 코드는 모델이 고칠 수 있게 알려 준다(값 없음)
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
        "summary": plan.summary, "iac_managed": plan.iac_managed, "warnings": list(plan.warnings),
        "phases": [
            {"id": p.id, "title": p.title, "purpose": p.purpose, "preconditions": list(p.preconditions), "gate": p.gate,
             "gate_locked": p.gate_locked, "gate_reason": p.gate_reason, "rollback": p.rollback,
             **({"gate_waiver": p.gate_waiver} if p.gate_waiver else {}),
             "steps": [step_hash(s) for s in p.steps]}
            for p in plan.phases
        ],
    }
    return hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()
