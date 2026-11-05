"""연습장(초안): plan을 확정하기 전에 자유롭게 고치는 작업대. 확정(confirm)한 절차만 plan으로 만들어 Apply로 보낸다.

- 초안은 틀려도 저장된다. 서버는 거부하는 대신 줄마다 검증 상태(`ok`·`warn`·`err`)와 이유 코드를 돌려준다(필수 옵션 누락, 가드 차단, 확인 절차 없음 등).
- 확정은 검증이 `ok`인 절차만 할 수 있고, 변경 절차와 그 확인 절차, 의존하는 절차는 함께 묶여 확정·취소된다.
- Apply로 보내는 순간(`promote`)에는 `build_plan`의 전체 검증(명령 가드, 확인 절차 짝, 의존 순서, 게이트 잠금)을 다시 한다. 가드는 약해지지 않는다.
- 입력 문장은 입력 가드와 마스킹을 거친다(저장되는 초안은 모두 마스킹된 텍스트). 삭제한 서버 추가 절차는 다시 끼워 넣지 않는다(`suppressed`).
- 완료·건너뜀 절차와 apply 진행 중인 확정 절차는 고칠 수 없다(잠금). 되돌리기는 최근 30번까지다.
- 명령 만들기 도우미: 문서 검색 후보(`catalog_search`), 옵션 값 후보(`suggest`: 인벤토리 이름), 조회로 채우기(`lookup`: 읽기 명령).
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.guards.command_guard import CommandGuard
from app.guards.refs import find_refs, output_not_json
from app.guards.input_guard import check_user_input
from app.guards.masking import Masker
from app.planner.schema import STEP_ID, Plan, PlanDraft, PhaseDraft, PollSpec, StepDraft, StepKind, build_plan
from app.session import states
from app.session.store import SessionStore

HISTORY_MAX = 30
# 문서의 `required`에 넣으면 검증기가 "실제 CLI에서는 선택"이라고 경고하는 것들(대안 옵션이 있어서 CLI는 선택으로 둔다). 명령 만들기에서는 사실상 필요한 값이다.
BUILDER_REQUIRED = {
    "aws:ec2:run-instances": ("--image-id",),
    "aws:ec2:authorize-security-group-ingress": ("--group-id",),
    "aws:ec2:revoke-security-group-ingress": ("--group-id",),
    "aws:ec2:delete-security-group": ("--group-id",),
}
# 옵션 이름 → 인벤토리 카테고리(값 후보로 이름 목록을 보여 준다)
OPTION_CATEGORY = {
    "--cluster-name": "CLUSTER", "--name": "CLUSTER", "--nodegroup-name": "NODEGROUP", "--auto-scaling-group-name": "ASG", "--auto-scaling-group-names": "ASG",
    "--bucket": "BUCKET", "--role-name": "ROLE", "--user-name": "USER", "--policy-name": "POLICY", "--namespace": "NAMESPACE", "-n": "NAMESPACE",
    "--launch-template-name": "LT",
}
LOOKUPS = {   # 조회로 채우기: 읽기 명령 하나와 결과에서 값을 뽑는 방법
    "vpc": ("aws ec2 describe-vpcs", "Vpcs", lambda x: (x["VpcId"], f"{x['VpcId']}  {'기본 VPC' if x.get('IsDefault') else ''}  {x.get('CidrBlock', '')}")),
    "subnet": ("aws ec2 describe-subnets", "Subnets", lambda x: (x["SubnetId"], f"{x['SubnetId']}  {x.get('AvailabilityZone', '')}  {'기본 서브넷' if x.get('DefaultForAz') else ''}  {x.get('CidrBlock', '')}")),
    "sg": ("aws ec2 describe-security-groups", "SecurityGroups", lambda x: (x["GroupId"], f"{x['GroupId']}  {x.get('GroupName', '')}  ({x.get('VpcId', '')})")),
}
AMI_PARAMS = {
    "ami_ubuntu": ("/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id", "Ubuntu 22.04"),
    "ami_al2023": ("/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64", "Amazon Linux 2023"),
}
EDITABLE_FIELDS = frozenset({"title", "command", "rationale", "kind", "depends_on", "check_for", "poll", "checklist", "irreversible"})
CHANGE_OPS = frozenset({"mutate", "manual"})


class ScratchError(Exception):
    def __init__(self, code: str, codes: list[str] | None = None):
        super().__init__(code)
        self.code, self.codes = code, codes or []


class DStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=STEP_ID)
    kind: StepKind = "cli"
    title: str = Field(default="", max_length=120)
    command: str | None = Field(default=None, max_length=600)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    check_for: str | None = Field(default=None, pattern=STEP_ID)
    poll: PollSpec | None = None
    checklist: list[str] = Field(default_factory=list, max_length=12)
    rationale: str = Field(default="", max_length=200)
    irreversible: bool = False
    confirmed: bool = False
    origin: Literal["agent", "auto", "user"] = "user"


class DPhase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=STEP_ID)
    title: str = Field(default="", max_length=100)
    purpose: str = Field(default="", max_length=200)
    preconditions: list[str] = Field(default_factory=list, max_length=8)
    gate: bool = False
    gate_reason: str = Field(default="", max_length=160)
    rollback: str = Field(default="", max_length=300)
    steps: list[DStep] = Field(default_factory=list)


class Scratch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phases: list[DPhase] = Field(default_factory=list)
    suppressed: list[str] = Field(default_factory=list)       # 사용자가 지운 서버 추가 절차의 id(다시 끼워 넣지 않는다)

    def steps(self) -> list[DStep]:
        return [s for p in self.phases for s in p.steps]

    def find(self, step_id: str) -> tuple[DPhase, int]:
        for p in self.phases:
            for i, s in enumerate(p.steps):
                if s.id == step_id:
                    return p, i
        raise ScratchError("unknown_step")


# 앞 절차의 결과로 뒤 절차가 쓸 수 있는 리소스 ID(화면의 \"앞 절차 결과 쓰기\" 목록용, D-48). 명령 앞부분이 같으면 적용한다. 실제 출력은 실행 때 서버가 검증한다.
REF_HINTS: dict[str, list[tuple[str, str]]] = {
    "aws ec2 create-security-group": [("GroupId", "보안 그룹 ID")],
    "aws ec2 run-instances": [("Instances[0].InstanceId", "인스턴스 ID")],
    "aws ec2 create-vpc": [("Vpc.VpcId", "VPC ID")],
    "aws ec2 create-subnet": [("Subnet.SubnetId", "서브넷 ID")],
    "aws ec2 create-internet-gateway": [("InternetGateway.InternetGatewayId", "인터넷 게이트웨이 ID")],
    "aws ec2 create-route-table": [("RouteTable.RouteTableId", "라우트 테이블 ID")],
    "aws ec2 create-nat-gateway": [("NatGateway.NatGatewayId", "NAT 게이트웨이 ID")],
    "aws ec2 create-volume": [("VolumeId", "볼륨 ID")],
    "aws ec2 allocate-address": [("AllocationId", "탄력적 IP 할당 ID")],
    "aws ec2 create-network-interface": [("NetworkInterface.NetworkInterfaceId", "네트워크 인터페이스 ID")],
    "aws ec2 create-snapshot": [("SnapshotId", "스냅샷 ID")],
    "aws ec2 create-launch-template": [("LaunchTemplate.LaunchTemplateId", "시작 템플릿 ID")],
}


def ref_fields(command: str | None) -> list[dict]:
    c = " ".join((command or "").split())
    for prefix, fields in REF_HINTS.items():
        if c == prefix or c.startswith(prefix + " "):
            return [{"path": p, "label": lab} for p, lab in fields]
    return []


class ScratchPad:
    def __init__(self, store: SessionStore, masker_for: Callable[[str], Masker], guard: CommandGuard, *, retriever: Any = None,
                 runner_for: Callable[[str], Any] | None = None):
        self._s, self._masker_for, self._guard = store, masker_for, guard
        self._retriever, self._runner_for = retriever, runner_for

    # ---------------- 저장 ----------------
    def get(self, sid: str) -> Scratch:
        raw = self._s.get_meta(sid, "scratch")
        return Scratch.model_validate_json(raw) if raw else Scratch()

    def _save(self, sid: str, sc: Scratch, *, snapshot: str | None = None) -> None:
        if snapshot is not None:
            hist = json.loads(self._s.get_meta(sid, "scratch_hist") or "[]")
            hist.append(snapshot)
            self._s.set_meta(sid, "scratch_hist", json.dumps(hist[-HISTORY_MAX:]))
            self._s.set_meta(sid, "scratch_redo", "[]")
        self._s.set_meta(sid, "scratch", sc.model_dump_json())

    def can_undo_redo(self, sid: str) -> tuple[bool, bool]:
        return bool(json.loads(self._s.get_meta(sid, "scratch_hist") or "[]")), bool(json.loads(self._s.get_meta(sid, "scratch_redo") or "[]"))

    # ---------------- 잠금 ----------------
    def locks(self, sid: str, sc: Scratch) -> dict[str, str]:
        """고칠 수 없는 절차: 완료·건너뜀(부분 재계획에서 잠김), apply 진행 중인 확정 절차."""
        out: dict[str, str] = {}
        v = self._s.latest_version(sid)
        if v is None:
            return out
        state = self._s.session(sid)["state"]
        st = self._s.step_states(sid, v)
        sent = {x.id for x in self._s.load_plan(sid, v).all_steps()}
        for s in sc.steps():
            info = st.get(s.id)
            if info and info["state"] in states.SATISFIED_STEP:
                out[s.id] = "done"
            elif state in ("applying",) and s.id in sent and s.confirmed:
                out[s.id] = "in_apply"
        return out

    def _writable(self, sid: str) -> None:
        if self._s.session(sid)["state"] in ("completed", "aborted"):
            raise ScratchError("session_closed")

    # ---------------- 연산 ----------------
    def op(self, sid: str, nick: str, m: dict) -> dict:
        """연습장 연산 하나를 적용한다. 거부는 `ScratchError`, 성공은 부가 정보 dict를 돌려준다."""
        self._writable(sid)
        name = m.get("op")
        if name in ("undo", "redo"):
            return self._undo_redo(sid, name)
        sc = self.get(sid)
        before = sc.model_dump_json()
        locks = self.locks(sid, sc)
        data: dict = {}
        fn = {"add_step": self._add_steps, "add_steps": self._add_steps, "update_step": self._update_step, "delete_step": self._delete_step,
              "move_step": self._move_step, "duplicate_step": self._duplicate, "set_confirmed": self._set_confirmed, "confirm_valid": self._confirm_valid,
              "add_phase": self._add_phase, "update_phase": self._update_phase, "delete_phase": self._delete_phase}.get(name)
        if fn is None:
            raise ScratchError("unknown_scratch_op")
        data = fn(sid, sc, m, locks) or {}
        self._save(sid, sc, snapshot=before)
        return data

    def _undo_redo(self, sid: str, which: str) -> dict:
        src, dst = ("scratch_hist", "scratch_redo") if which == "undo" else ("scratch_redo", "scratch_hist")
        stack = json.loads(self._s.get_meta(sid, src) or "[]")
        if not stack:
            raise ScratchError(f"nothing_to_{which}")
        cur = self.get(sid)
        target = Scratch.model_validate_json(stack[-1])
        locks = self.locks(sid, cur)
        for sid_, _ in locks.items():                             # 잠긴 절차는 되돌리기로도 바뀌지 않는다
            a = next((x for x in cur.steps() if x.id == sid_), None)
            b = next((x for x in target.steps() if x.id == sid_), None)
            if a is None or b is None or a.model_dump() != b.model_dump():
                raise ScratchError("undo_blocked_by_lock")
        other = json.loads(self._s.get_meta(sid, dst) or "[]")
        other.append(cur.model_dump_json())
        self._s.set_meta(sid, dst, json.dumps(other[-HISTORY_MAX:]))
        self._s.set_meta(sid, src, json.dumps(stack[:-1]))
        self._s.set_meta(sid, "scratch", target.model_dump_json())
        return {}

    # ---- 입력 정리 ----
    def _clean(self, sid: str, fields: dict) -> dict:
        masker = self._masker_for(sid)
        out = dict(fields)

        def one(label: str, text: str) -> str:
            if not text.strip():
                return text
            v = check_user_input(text, masker)
            if not v.allow or v.masked_text is None:
                raise ScratchError(f"input_blocked:{label}:{','.join(v.reasons[:2])}")
            return v.masked_text
        for f in ("title", "command", "rationale"):
            if isinstance(out.get(f), str):
                out[f] = one(f, out[f])
        if isinstance(out.get("checklist"), list):
            out["checklist"] = [one("checklist", str(x)) for x in out["checklist"]]
        if isinstance(out.get("poll"), dict) and isinstance(out["poll"].get("until"), str):
            out["poll"] = {**out["poll"], "until": one("poll", out["poll"]["until"])}
        return out

    @staticmethod
    def _new_id(sc: Scratch, base: str = "step") -> str:
        used = {s.id for s in sc.steps()} | set(sc.suppressed)
        n = 1
        while f"{base}-{n}" in used:
            n += 1
        return f"{base}-{n}"

    def _phase(self, sc: Scratch, phase_id: str | None) -> DPhase:
        if phase_id is None:
            if not sc.phases:
                sc.phases.append(DPhase(id="p1", title="작업"))
            return sc.phases[-1]
        for p in sc.phases:
            if p.id == phase_id:
                return p
        raise ScratchError("unknown_phase")

    def _check_unlocked(self, ids, locks: dict[str, str]) -> None:
        for i in ids:
            if i in locks:
                raise ScratchError("step_locked_done" if locks[i] == "done" else "step_in_apply")

    # ---- 절차 ----
    def _add_steps(self, sid: str, sc: Scratch, m: dict, locks: dict) -> dict:
        raw = m.get("steps") if m.get("op") == "add_steps" else [m.get("step") or {}]
        if not isinstance(raw, list) or not raw or len(raw) > 6:
            raise ScratchError("bad_steps")
        after = m.get("after")
        phase = sc.find(after)[0] if after and not m.get("phase") else self._phase(sc, m.get("phase"))
        idx = len(phase.steps)
        if after:
            ap, ai = sc.find(after)
            if ap.id != phase.id:
                raise ScratchError("after_not_in_phase")
            idx = ai + 1
        existing = {s.id for s in sc.steps()}
        made: list[DStep] = []
        for r in raw:
            if not isinstance(r, dict):
                raise ScratchError("bad_steps")
            r = self._clean(sid, {k: v for k, v in r.items() if k in EDITABLE_FIELDS | {"id"}})
            r.setdefault("id", self._new_id(sc))
            if r["id"] in existing or r["id"] in sc.suppressed:
                raise ScratchError(f"duplicate_step_id:{r['id']}")
            try:
                st = DStep(**r, origin="user")
            except ValidationError as e:
                raise ScratchError("invalid_step", [f"{'.'.join(str(x) for x in err['loc'])}:{err['type']}" for err in e.errors()[:4]]) from None
            existing.add(st.id)
            made.append(st)
        phase.steps[idx:idx] = made
        return {"added": [s.id for s in made]}

    def _update_step(self, sid: str, sc: Scratch, m: dict, locks: dict) -> dict:
        step_id = m.get("step_id")
        phase, i = sc.find(step_id)
        self._check_unlocked([step_id], locks)
        fields = m.get("fields") or {}
        bad = set(fields) - EDITABLE_FIELDS
        if bad:
            raise ScratchError(f"field_not_editable:{sorted(bad)[0]}")
        cur = phase.steps[i]
        clean = self._clean(sid, fields)
        try:
            new = DStep(**{**cur.model_dump(), **clean})
        except ValidationError as e:
            raise ScratchError("invalid_step", [f"{'.'.join(str(x) for x in err['loc'])}:{err['type']}" for err in e.errors()[:4]]) from None
        unconf: list[str] = []
        if cur.confirmed and any(getattr(new, k) != getattr(cur, k) for k in ("command", "kind", "check_for", "depends_on", "poll", "irreversible")):
            phase.steps[i] = new.model_copy(update={"confirmed": False})
            unconf = self._unconfirm_closure(sc, step_id, include_self=False)          # 내용이 바뀐 확정 절차는 확정이 풀리고 함께 묶인 절차도 푼다
            unconf = [step_id, *unconf]
        else:
            phase.steps[i] = new
        return {"unconfirmed": unconf}

    def _delete_step(self, sid: str, sc: Scratch, m: dict, locks: dict) -> dict:
        step_id = m.get("step_id")
        phase, i = sc.find(step_id)
        self._check_unlocked([step_id], locks)
        s = phase.steps[i]
        unconf = self._unconfirm_closure(sc, step_id, include_self=False) if s.confirmed else []
        del phase.steps[i]
        if s.origin == "auto" and step_id not in sc.suppressed:
            sc.suppressed.append(step_id)                             # 지운 서버 추가 절차는 다시 끼워 넣지 않는다
        return {"unconfirmed": unconf}

    def _move_step(self, sid: str, sc: Scratch, m: dict, locks: dict) -> dict:
        step_id = m.get("step_id")
        phase, i = sc.find(step_id)
        self._check_unlocked([step_id], locks)
        target = self._phase(sc, m.get("phase") or phase.id)
        step = phase.steps.pop(i)
        after = m.get("after")
        j = 0
        if after:
            ap, ai = sc.find(after)
            if ap.id != target.id:
                phase.steps.insert(i, step)
                raise ScratchError("after_not_in_phase")
            j = ai + 1
        target.steps.insert(j, step)
        return {}

    def _duplicate(self, sid: str, sc: Scratch, m: dict, locks: dict) -> dict:
        phase, i = sc.find(m.get("step_id"))
        src = phase.steps[i]
        new = src.model_copy(update={"id": self._new_id(sc, "copy"), "title": (src.title + " (복제)")[:120], "confirmed": False, "origin": "user", "check_for": src.check_for})
        phase.steps.insert(i + 1, new)
        return {"added": [new.id]}

    # ---- 확정 ----
    def _group(self, sc: Scratch, ids: set[str]) -> set[str]:
        """함께 확정되어야 하는 절차: 의존·확인 대상(앞), 변경 절차의 확인 절차(뒤)."""
        by = {s.id: s for s in sc.steps()}
        out = set(ids)
        changed = True
        while changed:
            changed = False
            for i in list(out):
                s = by.get(i)
                if s is None:
                    continue
                for ref in (*s.depends_on, *([s.check_for] if s.check_for else []), *[r for r, _ in find_refs(s.command)]):
                    if ref in by and ref not in out:
                        out.add(ref)
                        changed = True
                for t in by.values():
                    if t.check_for == i and t.id not in out:
                        out.add(t.id)
                        changed = True
        return out

    def _unconfirm_closure(self, sc: Scratch, step_id: str, include_self: bool = True) -> list[str]:
        """확정을 풀 때 함께 풀어야 하는 절차(이 절차에 의존하는 것, 이 절차를 확인하는 것, 이 절차가 확인하는 대상)."""
        by = {s.id: s for s in sc.steps()}
        out = {step_id}
        changed = True
        while changed:
            changed = False
            for t in by.values():
                if t.id in out:
                    continue
                if any(d in out for d in t.depends_on) or (t.check_for and t.check_for in out) or any(r in out for r, _ in find_refs(t.command)):
                    out.add(t.id)
                    changed = True
            for i in list(out):
                s = by.get(i)
                if s and s.check_for and s.check_for in by and s.check_for not in out:
                    out.add(s.check_for)
                    changed = True
        if not include_self:
            out.discard(step_id)
        affected = []
        for i in out:
            if i in by and by[i].confirmed:
                by[i].confirmed = False
                affected.append(i)
        return sorted(affected)

    def _set_confirmed(self, sid: str, sc: Scratch, m: dict, locks: dict) -> dict:
        ids = m.get("step_ids") or []
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise ScratchError("bad_step_ids")
        by = {s.id: s for s in sc.steps()}
        for i in ids:
            if i not in by:
                raise ScratchError("unknown_step")
        if m.get("value"):
            status = self.validate(sid, sc)
            group = self._group(sc, set(ids))
            for gid in sorted(group):
                st = status.get(gid, {})
                if st.get("status") != "ok":
                    raise ScratchError(f"not_confirmable:{gid}", [f"{x['code']}" for x in st.get("issues", [])][:4])
            for gid in group:
                by[gid].confirmed = True
            return {"confirmed": sorted(group)}
        self._check_unlocked(ids, locks)
        out: list[str] = []
        for i in ids:
            out += self._unconfirm_closure(sc, i)
        self._check_unlocked(out, locks)
        return {"unconfirmed": sorted(set(out))}

    def _confirm_valid(self, sid: str, sc: Scratch, m: dict, locks: dict) -> dict:
        """검증이 ok인 절차를 한꺼번에 확정한다(묶음 전체가 ok일 때만)."""
        status = self.validate(sid, sc)
        by = {s.id: s for s in sc.steps()}
        done: set[str] = set()
        for s in sc.steps():
            if s.confirmed or status[s.id]["status"] != "ok":
                continue
            group = self._group(sc, {s.id})
            if all(status.get(g, {}).get("status") == "ok" for g in group):
                for g in group:
                    by[g].confirmed = True
                done |= group
        return {"confirmed": sorted(done)}

    # ---- 단계 ----
    def _add_phase(self, sid: str, sc: Scratch, m: dict, locks: dict) -> dict:
        title = self._clean(sid, {"title": str(m.get("title") or "새 단계")})["title"][:100]
        n = 1
        while any(p.id == f"p{n}" for p in sc.phases):
            n += 1
        sc.phases.append(DPhase(id=f"p{n}", title=title))
        return {"phase": f"p{n}"}

    def _update_phase(self, sid: str, sc: Scratch, m: dict, locks: dict) -> dict:
        ph = self._phase(sc, m.get("phase"))
        fields = m.get("fields") or {}
        allowed = {"title", "purpose", "preconditions", "gate", "gate_reason", "rollback"}
        if set(fields) - allowed:
            raise ScratchError("field_not_editable")
        masker = self._masker_for(sid)
        data = ph.model_dump()
        for k, v in fields.items():
            if isinstance(v, str) and v.strip():
                chk = check_user_input(v, masker)
                if not chk.allow or chk.masked_text is None:
                    raise ScratchError(f"input_blocked:{k}:{','.join(chk.reasons[:2])}")
                v = chk.masked_text
            if k == "preconditions":
                v = [(check_user_input(x, masker).masked_text or "") for x in (v or []) if isinstance(x, str) and x.strip()]
            data[k] = v
        try:
            new = DPhase(**data)
        except ValidationError:
            raise ScratchError("invalid_phase") from None
        ph.title, ph.purpose, ph.preconditions, ph.gate, ph.gate_reason, ph.rollback = new.title, new.purpose, new.preconditions, new.gate, new.gate_reason, new.rollback
        return {}

    def _delete_phase(self, sid: str, sc: Scratch, m: dict, locks: dict) -> dict:
        ph = self._phase(sc, m.get("phase"))
        self._check_unlocked([s.id for s in ph.steps], locks)
        unconf: list[str] = []
        for s in ph.steps:
            if s.confirmed:
                unconf += self._unconfirm_closure(sc, s.id, include_self=False)
            if s.origin == "auto" and s.id not in sc.suppressed:
                sc.suppressed.append(s.id)
        sc.phases = [p for p in sc.phases if p.id != ph.id]
        return {"unconfirmed": sorted(set(unconf))}

    # ---- aws eks → kubectl ----
    # ---------------- 검증(초안은 거부하지 않고 상태를 알려 준다) ----------------
    def validate(self, sid: str, sc: Scratch | None = None) -> dict[str, dict]:
        sc = sc or self.get(sid)
        masker = self._masker_for(sid)
        order = {s.id: n for n, s in enumerate(sc.steps())}
        phase_of = {s.id: p.id for p in sc.phases for s in p.steps}
        info: dict[str, dict] = {}
        for p in sc.phases:
            for s in p.steps:
                issues: list[dict] = []
                v = None
                op = "none"
                if s.kind in ("cli", "manual", "poll") and not (s.command or "").strip():
                    issues.append({"sev": "err", "code": "command_required"})
                elif (s.command or "").strip():
                    v = self._guard.check(s.command, masker)
                    if not v.allow:
                        issues += [{"sev": "err", "code": "blocked", "detail": b} for b in v.blocks[:3]]
                    else:
                        op = v.kind
                        if (s.kind == "manual") != (v.kind == "manual") and s.kind in ("manual", "cli", "poll"):
                            issues.append({"sev": "err", "code": "kind_mismatch"})
                        if s.kind in ("poll", "check") and v.kind != "read":
                            issues.append({"sev": "err", "code": "must_be_read"})
                        if s.check_for and v.kind != "read":
                            issues.append({"sev": "err", "code": "must_be_read"})
                        chunk = self._chunk(v.chunk_id)
                        required = BUILDER_REQUIRED.get(v.chunk_id or "", ()) or (chunk.required if chunk else ())
                        try:
                            toks = shlex.split(s.command)
                        except ValueError:
                            toks = []
                        for opt in required:
                            if not any(t == opt or t.startswith(opt + "=") for t in toks):
                                issues.append({"sev": "warn", "code": "missing_param", "detail": opt})
                if s.kind in ("iac", "check") and not (s.command or "").strip() and not [x for x in s.checklist if x.strip()]:
                    issues.append({"sev": "warn", "code": "checklist_required"})          # 사람이 할 일을 적지 않은 수동 절차
                if not s.title.strip():
                    issues.append({"sev": "warn", "code": "title_required"})
                if s.kind == "poll" and s.poll is None:
                    issues.append({"sev": "err", "code": "poll_spec_required"})
                if s.kind != "poll" and s.poll is not None:
                    issues.append({"sev": "err", "code": "poll_spec_unexpected"})
                used = find_refs(s.command)
                for rid, _path in used:                                                  # 앞 절차 결과 참조(D-48)
                    tgt = next((t for t in sc.steps() if t.id == rid), None)
                    if s.kind not in ("cli", "poll"):
                        issues.append({"sev": "err", "code": "ref_not_allowed_here"})
                    elif tgt is None:
                        issues.append({"sev": "err", "code": "ref_unknown_step", "detail": rid})
                    elif order[rid] >= order[s.id]:
                        issues.append({"sev": "warn", "code": "ref_order", "detail": rid})
                    elif tgt.kind not in ("cli", "poll") or not (tgt.command or "").strip():
                        issues.append({"sev": "err", "code": "ref_target_not_runnable", "detail": rid})
                    elif output_not_json(tgt.command):
                        issues.append({"sev": "warn", "code": "ref_target_not_json", "detail": rid})
                for ref in (*s.depends_on, *([s.check_for] if s.check_for else [])):
                    if ref not in order:
                        issues.append({"sev": "err", "code": "unknown_ref", "detail": ref})
                    elif order[ref] >= order[s.id]:
                        issues.append({"sev": "warn", "code": "order", "detail": ref})
                info[s.id] = {"uses": sorted({r for r, _ in used}), "ref_fields": ref_fields(s.command), "op": op, "risk": getattr(v, "risk", "low") if v else "low", "grounded": bool(v and v.grounded), "flags": list(v.flags) if v else [],
                              "requires": list(v.requires) if v else [], "chunk_id": v.chunk_id if v else None, "issues": issues,
                              "irreversible": bool(s.irreversible or (v and v.impact.get("reversible") == "irreversible"))}
        for p in sc.phases:                                                           # 구조: 변경 절차는 뒤에 확인 절차가 있어야 한다, 확인 대상은 변경이어야 한다
            for n, s in enumerate(p.steps):
                it = info[s.id]
                if it["op"] == "mutate" and not any(t.check_for == s.id for t in p.steps[n + 1:]):
                    it["issues"].append({"sev": "warn", "code": "missing_check"})
                if s.check_for and s.check_for in info:
                    tgt = next(t for t in sc.steps() if t.id == s.check_for)
                    if not (info[tgt.id]["op"] in CHANGE_OPS or tgt.kind == "iac"):
                        it["issues"].append({"sev": "err", "code": "check_target_not_change", "detail": tgt.id})
        for sid_, it in info.items():
            sev = {i["sev"] for i in it["issues"]}
            it["status"] = "err" if "err" in sev else "warn" if "warn" in sev else "ok"
        return info

    def _chunk(self, chunk_id: str | None):
        if not chunk_id:
            return None
        cli, service, name = chunk_id.split(":", 2)
        return self._guard.index.by_key.get((cli, service, name))

    # ---------------- plan으로 승격 ----------------
    def promote(self, sid: str, iac_managed: bool) -> Plan:
        """확정한 절차만 plan으로 만든다. `build_plan`의 전체 검증을 다시 거친다(실패하면 `PlanInvalid`)."""
        sc = self.get(sid)
        phases = []
        for p in sc.phases:
            steps = [StepDraft(id=s.id, kind=s.kind, title=s.title, command=s.command, depends_on=s.depends_on, check_for=s.check_for, poll=s.poll,
                               checklist=s.checklist, rationale=s.rationale, irreversible=s.irreversible) for s in p.steps if s.confirmed]
            if steps:
                phases.append(PhaseDraft(id=p.id, title=p.title or p.id, purpose=p.purpose, preconditions=p.preconditions, steps=steps, gate=p.gate,
                                         gate_reason=p.gate_reason, rollback=p.rollback))
        if not phases:
            raise ScratchError("nothing_confirmed")
        summary = self._s.get_meta(sid, "plan_summary") or "확정한 절차"
        masker = self._masker_for(sid)
        frozen = frozenset(i for i, why in self.locks(sid, sc).items() if why == "done")      # 끝난 절차에는 자동 삽입을 하지 않는다(부분 재계획에서 잠긴 절차가 바뀌지 않게)
        return build_plan(PlanDraft(summary=summary[:300], phases=phases), self._guard, masker, iac_managed=iac_managed, suppressed=frozenset(sc.suppressed), frozen=frozen)

    def sync_from_plan(self, sid: str, plan: Plan) -> None:
        """승격한 plan을 연습장에 맞춘다: 서버가 넣은 절차는 연습장에도 나타나 고치거나 지울 수 있고, 의존 관계도 plan과 같아진다."""
        sc = self.get(sid)
        have = {s.id for s in sc.steps()}
        for pp in plan.phases:
            dp = next((p for p in sc.phases if p.id == pp.id), None)
            if dp is None:
                continue
            prev: str | None = None
            for s in pp.steps:
                if s.id in have:
                    dstep = next(x for x in dp.steps if x.id == s.id)
                    dstep.depends_on = list(s.depends_on)
                else:
                    new = DStep(id=s.id, kind=s.kind, title=s.title, command=s.command, depends_on=list(s.depends_on), check_for=s.check_for, poll=s.poll,
                                checklist=list(s.checklist), rationale=s.rationale, irreversible=s.declared_irreversible, confirmed=True, origin="auto" if s.auto else "agent")
                    idx = 0 if prev is None else next((i for i, x in enumerate(dp.steps) if x.id == prev), -1) + 1
                    dp.steps.insert(idx, new)
                    have.add(s.id)
                prev = s.id
        self._save(sid, sc)

    def import_plan(self, sid: str, plan: Plan) -> None:
        """에이전트가 만든 plan으로 연습장을 채운다(모두 확정된 상태: 이미 plan으로 제출되었다)."""
        phases = []
        for p in plan.phases:
            steps = [DStep(id=s.id, kind=s.kind, title=s.title, command=s.command, depends_on=list(s.depends_on), check_for=s.check_for, poll=s.poll,
                           checklist=list(s.checklist), rationale=s.rationale, irreversible=s.declared_irreversible, confirmed=True, origin="auto" if s.auto else "agent")
                     for s in p.steps]
            phases.append(DPhase(id=p.id, title=p.title, purpose=p.purpose, preconditions=list(p.preconditions), gate=p.gate and not p.gate_locked,
                                 gate_reason=p.gate_reason, rollback=p.rollback, steps=steps))
        self._s.set_meta(sid, "plan_summary", plan.summary)
        self._save(sid, Scratch(phases=phases))

    def import_as_drafts(self, sid: str, plan: Plan) -> list[str]:
        """에이전트가 만든 계획을 확정하지 않은 초안으로 연습장에 덧붙인다(이미 있는 초안은 그대로 둔다). 겹치는 id는 바꾼다."""
        sc = self.get(sid)
        used = {s.id for s in sc.steps()} | set(sc.suppressed)
        used_p = {p.id for p in sc.phases}
        smap: dict[str, str] = {}
        for p in plan.phases:
            for s in p.steps:
                new, n = s.id, 1
                while new in used:
                    n += 1
                    new = f"{s.id[:34]}-g{n}"
                smap[s.id] = new
                used.add(new)
        added = []
        for p in plan.phases:
            pid, n = p.id, 1
            while pid in used_p:
                n += 1
                pid = f"{p.id[:34]}-g{n}"
            used_p.add(pid)
            steps = [DStep(id=smap[s.id], kind=s.kind, title=s.title, command=s.command, depends_on=[smap[d] for d in s.depends_on if d in smap],
                           check_for=smap.get(s.check_for) if s.check_for else None, poll=s.poll, checklist=list(s.checklist), rationale=s.rationale,
                           irreversible=s.declared_irreversible, confirmed=False, origin="auto" if s.auto else "agent") for s in p.steps]
            added += [x.id for x in steps]
            sc.phases.append(DPhase(id=pid, title=p.title, purpose=p.purpose, preconditions=list(p.preconditions), gate=p.gate and not p.gate_locked,
                                    gate_reason=p.gate_reason, rollback=p.rollback, steps=steps))
        self._save(sid, sc, snapshot=self.get(sid).model_dump_json())
        return added

    def dirty(self, sid: str) -> bool:
        """연습장의 확정 절차가 마지막으로 보낸 plan과 다른가(다르면 Apply로 다시 보내야 한다)."""
        v = self._s.latest_version(sid)
        sc = self.get(sid)
        mine = [(p.id, s.id, s.command, s.title, s.kind, tuple(s.checklist)) for p in sc.phases for s in p.steps if s.confirmed]
        if v is None:
            return bool(mine)
        plan = self._s.load_plan(sid, v)
        sent = [(p.id, s.id, s.command, s.title, s.kind, tuple(s.checklist)) for p in plan.phases for s in p.steps]
        return mine != sent

    # ---------------- 명령 만들기 도우미 ----------------
    def catalog_search(self, sid: str, query: str) -> list[dict]:
        if self._retriever is None:
            return []
        masker = self._masker_for(sid)
        v = check_user_input(query, masker)
        if not v.allow or v.masked_text is None:
            raise ScratchError("input_rejected")
        res = self._retriever.search(v.masked_text, k=6, method="bm25")
        out = []
        for h in res.hits:
            c = h.chunk
            ex = re.search(r"예시:\s*`([^`]+)`", c.text)
            req = BUILDER_REQUIRED.get(c.id) or c.required
            out.append({"id": c.id, "command": c.command, "kind": c.kind, "risk": c.risk, "required": list(req),
                        "optional": [o for o in c.optional if o not in req], "topic": c.topic, "example": ex.group(1) if ex else ""})
        return out

    def suggest(self, sid: str, option: str) -> list[str]:
        cat = OPTION_CATEGORY.get(option)
        return list(self._masker_for(sid).inventory.names.get(cat, [])) if cat else []

    def lookup(self, sid: str, kind: str, vpc: str = "") -> list[dict]:
        """읽기 명령으로 값 후보(VPC, 서브넷, 보안 그룹, AMI)를 조회한다. 결과는 화면에만 간다(실제 값)."""
        if self._runner_for is None:
            raise ScratchError("lookup_unavailable")
        runner = self._runner_for(sid)
        try:
            if kind in AMI_PARAMS:
                name, label = AMI_PARAMS[kind]
                out = json.loads(runner.run(f"aws ssm get-parameter --name {name}", timeout=30))
                val = out["Parameter"]["Value"]
                return [{"value": val, "label": f"{val}  {label}"}]
            if kind not in LOOKUPS:
                raise ScratchError("unknown_lookup")
            cmd, key, fmt = LOOKUPS[kind]
            if vpc and kind in ("subnet", "sg") and re.fullmatch(r"vpc-[0-9a-f]{8,17}", vpc):
                cmd += f" --filters Name=vpc-id,Values={vpc}"
            data = json.loads(runner.run(cmd, timeout=30))[key]
            return [{"value": v, "label": " ".join(l.split())} for v, l in (fmt(x) for x in data[:50])]
        except (ValueError, KeyError, TypeError):
            raise ScratchError("lookup_failed") from None
