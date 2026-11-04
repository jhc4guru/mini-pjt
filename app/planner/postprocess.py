"""계획 후처리: 문서의 사전 점검(`impact.preflight`)과 IaC 동기화를 서버가 보장한다.

모델이 만든 계획에 서버가 step을 더한다(모델에 맡기지 않는다). 더한 step은 `Step.auto=True`로 표시하고, 승인·해시·표시에서 일반 step과 같이 다룬다.

- 사전 점검: 변경 step의 `impact.preflight` 명령이 그 변경 앞에 읽기 step으로 없으면, 변경 명령의 옵션·대상에서 인자를 가져와
  읽기 명령을 만들어 그 phase 맨 앞에 넣는다(plan 단계에서 실행되어 결과가 표시된다). 만든 명령은 명령 가드를 통과한 문서 근거의
  읽기 명령일 때만 쓴다. 인자를 정할 수 없거나(복사할 옵션·대상이 없음) 가드가 허용하지 않으면 값을 지어내지 않고 경고
  `preflight_not_inserted:<id>:<명령>`만 남긴다. 이미 같은 점검(같은 문서 명령의 읽기 step)이 앞에 있으면 넣지 않는다.
- `dry_run`이 문서에 있으면 경고 `dry_run_available:<id>`로 알린다(변경 명령 자체를 바꾸지 않는다).
- IaC 동기화: `iac_managed`이고 `impact.iac: true`인 변경 step은 확인 step 뒤에 "코드 동기화 확인" `iac` step을 넣고 경고
  `iac_drift:<id>`, `consider_iac_step:<id>`(CLI 직접 변경 대신 코드 변경으로 적용 가능한지 검토)를 남긴다. 이미 그 변경에
  의존하는 `iac` step이 있으면 넣지 않는다.
- 더한 뒤 plan·phase 크기 상한을 다시 검사한다(넘으면 `PlanInvalid`).
"""
from __future__ import annotations

import re

from app.guards.agent_limits import PlanTooLarge, check_plan_size
from app.guards.command_guard import CommandGuard, _split
from app.guards.masking import Masker
from app.planner.schema import Plan, PlanInvalid, Step

_NODE_COMMANDS = frozenset({"cordon", "uncordon", "drain", "taint"})   # 위치 인자가 노드 이름뿐인 변경 명령
_KUBECTL_COPY_OPTS = {"--namespace": "--namespace", "-n": "--namespace"}


def apply_postprocess(plan: Plan, guard: CommandGuard, masker: Masker) -> Plan:
    warnings: list[str] = []
    phases = [list(p.steps) for p in plan.phases]
    ids = {s.id for p in phases for s in p}
    commands: set[str] = set()                         # 이미 더한 사전 점검 명령(같은 명령을 두 번 넣지 않는다)

    # ---- 사전 점검 ----
    seen_reads: set[str] = set()                       # 지금까지(변경 앞) 나온 읽기 step의 문서 명령 id
    inserted: dict[int, list[Step]] = {}
    for pi, steps in enumerate(phases):
        for si, s in enumerate(steps):
            if s.op == "read" and s.chunk_id:
                seen_reads.add(s.chunk_id)
            if s.op != "mutate":
                continue
            if s.impact.get("dry_run"):
                warnings.append(f"dry_run_available:{s.id}")
            new_deps: list[str] = []
            for spec in s.impact.get("preflight") or []:
                chunk = _resolve(guard, s, str(spec))
                if chunk is None:
                    warnings.append(f"preflight_not_inserted:{s.id}:{spec}")
                    continue
                if chunk.id in seen_reads:
                    continue
                cmd = _build_preflight(guard, s, chunk)
                v = guard.check(cmd, masker) if cmd else None
                if cmd is None or v is None or not (v.allow and v.kind == "read" and v.grounded and v.chunk_id == chunk.id):
                    warnings.append(f"preflight_not_inserted:{s.id}:{spec}")
                    continue
                seen_reads.add(chunk.id)
                if cmd in commands:
                    continue
                commands.add(cmd)
                sid = _unique(f"pre-{s.id}", ids)
                ids.add(sid)
                new_deps.append(sid)
                inserted.setdefault(pi, []).append(Step(
                    id=sid, kind="cli", title=f"사전 점검: {chunk.command}", command=cmd,
                    rationale=f"자동 삽입: {s.id}의 사전 점검(문서 impact.preflight)", auto=True,
                    op=v.kind, risk=v.risk, grounded=v.grounded, chunk_id=v.chunk_id, flags=v.flags, requires=v.requires, impact=dict(v.impact),
                ))
            if new_deps:
                steps[si] = s.model_copy(update={"depends_on": (*s.depends_on, *new_deps)})
    for pi, extra in inserted.items():
        phases[pi] = [*extra, *phases[pi]]

    # ---- IaC 동기화 ----
    if plan.iac_managed:
        for steps in phases:
            for s in [x for x in steps if x.op == "mutate" and x.impact.get("iac")]:
                warnings += [f"iac_drift:{s.id}", f"consider_iac_step:{s.id}"]
                if any(t.kind == "iac" and s.id in t.depends_on for p in phases for t in p):
                    continue
                idx = next(i for i, t in enumerate(steps) if t.id == s.id)
                last = max((i for i, t in enumerate(steps) if t.check_for == s.id), default=idx)
                sid = _unique(f"iac-{s.id}", ids)
                ids.add(sid)
                steps.insert(last + 1, Step(
                    id=sid, kind="iac", title=f"코드 동기화 확인: {s.id}", depends_on=(s.id,), auto=True,
                    checklist=(
                        f"CLI로 바꾼 내용({s.chunk_id or s.id})을 Terraform 코드에 반영하고 PR을 만든다",
                        "terraform plan에서 코드와 실제 상태의 차이(드리프트)가 없는지 확인한다",
                        "가능하면 이 CLI 변경 대신 코드 변경으로 적용할 수 있는지 검토한다",
                    ),
                    rationale="자동 삽입: iac_managed 환경에서 CLI 변경은 코드와 동기화되어야 한다", flags=("iac_drift",),
                ))

    try:
        check_plan_size([len(p) for p in phases])
    except PlanTooLarge:
        raise PlanInvalid(["plan_too_large_after_auto_steps"]) from None
    new_phases = tuple(p.model_copy(update={"steps": tuple(steps)}) for p, steps in zip(plan.phases, phases))
    return plan.model_copy(update={"phases": new_phases, "warnings": tuple(dict.fromkeys([*plan.warnings, *warnings]))})


# ---------------- 사전 점검 명령 만들기 ----------------
def _unique(base: str, ids: set[str]) -> str:
    base = re.sub(r"[^a-z0-9-]", "-", base.lower())[:28].rstrip("-")
    cand, n = base, 1
    while cand in ids:
        n += 1
        cand = f"{base}-{n}"
    return cand


def _resolve(guard: CommandGuard, mutate: Step, spec: str):
    """`describe-x`(같은 CLI·서비스) 또는 `서비스:describe-x`(aws 다른 서비스)를 문서 chunk로 바꾼다."""
    cli, service, _ = (mutate.chunk_id or "::").split(":", 2)
    if cli == "aws" and ":" in spec:
        service, spec = spec.split(":", 1)
    return guard.index.by_key.get((cli, service, spec))


def _args(tokens: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    opts: dict[str, list[str]] = {}
    pos: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-") and not re.fullmatch(r"-\d+", t):
            name, eq, val = t.partition("=")
            vals = [val] if eq else []
            while not eq and i + 1 < len(tokens) and (not tokens[i + 1].startswith("-") or re.fullmatch(r"-\d+", tokens[i + 1])):
                vals.append(tokens[i + 1])
                i += 1
            opts[name] = vals
        else:
            pos.append(t)
        i += 1
    return opts, pos


def _build_preflight(guard: CommandGuard, mutate: Step, target) -> str | None:
    """변경 명령의 옵션·대상에서 인자를 가져와 읽기 명령을 만든다. 정할 수 없으면 None(값을 지어내지 않는다)."""
    tokens, _ = _split(mutate.command or "")
    if not tokens:
        return None
    if target.cli == "aws":
        return _aws_preflight(guard, tokens, target)
    return _kubectl_preflight(mutate, tokens, target)


def _aws_preflight(guard: CommandGuard, tokens: list[str], target) -> str | None:
    if len(tokens) < 4:
        return None
    rest = tokens[3:]
    opts, pos = _args(rest)
    if target.service == "s3" and target.name == "ls":            # aws s3 ls <s3 경로>
        uri = next((p for p in pos if p.startswith("s3://")), None)
        return f"aws s3 ls {uri}" if uri else None
    spec = guard.snapshot.get("aws", target.service, target.name)
    if spec is None:
        return None
    copied = [(o, v) for o, v in opts.items() if o in spec.options and v]
    if not copied:                                               # 복사할 대상이 없으면 범위가 너무 넓은 조회가 된다
        return None
    parts = ["aws", target.service, target.name]
    for o, v in copied:
        parts += [o, *v]
    return " ".join(parts)


def _kubectl_preflight(mutate: Step, tokens: list[str], target) -> str | None:
    name_words = len((mutate.chunk_id or "").split(":", 2)[-1].split())
    opts, pos = _args(tokens[1 + name_words:])
    pos = [p for p in pos if "=" not in p]                       # 라벨·이미지 지정(key=value)은 대상이 아니다
    if not pos:
        return None
    mname = (mutate.chunk_id or "").split(":", 2)[-1]
    if mname in _NODE_COMMANDS:
        pos = ["node", pos[0]]
    parts = ["kubectl", *target.name.split(), *pos]
    for o, canonical in _KUBECTL_COPY_OPTS.items():
        if o in opts and opts[o]:
            parts += [canonical, opts[o][0]]
            break
    return " ".join(parts)
