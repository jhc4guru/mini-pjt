"""시나리오 묶음(D-72): 반복되는 큰 작업(EKS 업그레이드 등)의 조사 목록과 phase 골격을 사람이 검토해 `knowledge/scenarios/`에 둔다.

요청이 시나리오에 맞으면(서버 규칙. 모델 판단이 아니다) 계획 에이전트는 모델 왕복 없이
- 서버가 시나리오의 읽기 명령을 일괄 실행하고(기존 `ReadToolbox` 관문 그대로),
- 윤곽(phase·게이트·롤백)을 시나리오에서 가져오며,
- 모델은 phase마다 step만 쓴다(시나리오의 안내와 필수 항목 `expect`를 서버가 검사해 모자라면 고치게 한다).
시나리오는 제안의 재료일 뿐이다. 명령은 여전히 `CommandGuard`, `build_plan` 전체 검증, 4-eyes 승인을 거친다.
시나리오에 쓰는 값은 마스킹된 요청에서 뽑은 플레이스홀더와 버전뿐이다(실제 값 없음). 규격은 `knowledge/scenarios/SCHEMA.md`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import ROOT

SCENARIO_DIR = ROOT / "knowledge" / "scenarios"
PLACEHOLDER = r"<{cat}_\d+>"
_TEMPLATE_PARAM = re.compile(r"\{(\w+)\}")
MAX_INVESTIGATION = 20          # 일괄 조사 한 번의 상한(`prompts.INV_MAX_COMMANDS`와 같다)
MAX_DOCS_PER_PHASE = 6


class ScenarioError(ValueError):
    """시나리오 파일이 규격에 맞지 않는다(로드 단계에서 바로 알린다)."""


@dataclass(frozen=True)
class ParamSpec:
    placeholder: str = ""                                  # 마스킹된 요청에서 첫 `<CATEGORY_n>`을 값으로 쓴다
    patterns: tuple[str, ...] = ()                         # 정규식(첫 캡처 그룹이 값). 위가 비었을 때 쓴다
    required: bool = False


@dataclass(frozen=True)
class ScenarioPhase:
    id: str
    title: str
    purpose: str = ""
    preconditions: tuple[str, ...] = ()
    gate: bool = False
    gate_reason: str = ""
    rollback: str = ""
    guidance: str = ""                                     # 이 phase를 쓰는 모델에게 주는 안내(`{param}` 치환)
    docs: tuple[str, ...] = ()                             # 서버가 미리 검색해 본문을 싣는 문서 질의(명령 이름 등)
    expect: tuple[tuple[str, ...], ...] = ()               # 필수 항목: 바깥은 AND, 안쪽은 OR. 정규식을 step의 "종류 제목 명령"에 검색한다


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    summary: str                                           # `{param}` 치환
    services_any: tuple[str, ...] = ()
    all_of: tuple[tuple[str, ...], ...] = ()               # 바깥은 AND, 안쪽은 OR(소문자 키워드)
    none_of: tuple[str, ...] = ()
    params: dict[str, ParamSpec] = field(default_factory=dict)
    investigation: tuple[str, ...] = ()                    # 읽기 명령 템플릿. 필요한 param이 없으면 그 항목은 건너뛴다
    phases: tuple[ScenarioPhase, ...] = ()


@dataclass(frozen=True)
class ScenarioMatch:
    scenario: Scenario
    params: dict[str, str]

    def fill(self, template: str) -> str:
        """`{param}`을 값으로 바꾼다. 값이 없으면 `(미지정)`."""
        return _TEMPLATE_PARAM.sub(lambda m: self.params.get(m.group(1), "(미지정)"), template)

    def commands(self) -> list[str]:
        """필요한 param이 모두 있는 조사 명령만, 중복 없이."""
        out: list[str] = []
        for tpl in self.scenario.investigation:
            if all(p in self.params for p in _TEMPLATE_PARAM.findall(tpl)):
                cmd = _TEMPLATE_PARAM.sub(lambda m: self.params[m.group(1)], tpl)
                if cmd not in out:
                    out.append(cmd)
        return out

    def phase(self, phase_id: str) -> ScenarioPhase | None:
        return next((p for p in self.scenario.phases if p.id == phase_id), None)


# ---------------- 로드 ----------------
def _tup(v) -> tuple:
    return tuple(v) if v else ()


def _groups(v) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(str(x).lower() for x in g) for g in (v or ()))


def parse_scenario(data: dict, source: str = "") -> Scenario:
    try:
        m = data.get("match") or {}
        params = {k: ParamSpec(str(v.get("placeholder") or ""), tuple(v.get("patterns") or ()), bool(v.get("required", False)))
                  for k, v in (data.get("params") or {}).items()}
        phases = tuple(
            ScenarioPhase(
                id=str(p["id"]), title=str(p["title"]), purpose=str(p.get("purpose", "")), preconditions=_tup(p.get("preconditions")),
                gate=bool(p.get("gate", False)), gate_reason=str(p.get("gate_reason", "")), rollback=str(p.get("rollback", "")),
                guidance=str(p.get("guidance", "")).strip(), docs=_tup(p.get("docs")),
                expect=tuple(tuple(str(x) for x in g) for g in (p.get("expect") or ())),
            ) for p in data["phases"]
        )
        sc = Scenario(
            id=str(data["id"]), title=str(data["title"]), summary=str(data["summary"]), services_any=_tup(m.get("services_any")),
            all_of=_groups(m.get("all_of")), none_of=tuple(str(x).lower() for x in (m.get("none_of") or ())), params=params,
            investigation=_tup(data.get("investigation")), phases=phases,
        )
    except (KeyError, TypeError, AttributeError) as e:
        raise ScenarioError(f"{source}: 필수 항목이 없거나 형식이 틀렸다({type(e).__name__}: {e})") from None
    problems = validate_structure(sc)
    if problems:
        raise ScenarioError(f"{source}: " + "; ".join(problems))
    return sc


def load_scenarios(directory: Path | None = None) -> list[Scenario]:
    d = directory or SCENARIO_DIR
    if not d.exists():
        return []
    out = []
    for path in sorted(d.glob("*.yaml")):
        out.append(parse_scenario(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, path.name))
    return out


def validate_structure(sc: Scenario) -> list[str]:
    """파일 안에서 알 수 있는 오류(문서·가드와의 대조는 테스트가 한다)."""
    from app.planner.schema import STEP_ID
    bad: list[str] = []
    ids = [p.id for p in sc.phases]
    if not sc.phases or len(set(ids)) != len(ids):
        bad.append("phase가 없거나 id가 겹친다")
    if len(sc.phases) > 20:
        bad.append("phase는 20개까지")
    for p in sc.phases:
        if not re.match(STEP_ID, p.id):
            bad.append(f"phase id 형식: {p.id}")
        if p.gate and not p.gate_reason:
            bad.append(f"게이트에는 사유가 필요하다: {p.id}")
        if len(p.title) > 100 or len(p.purpose) > 200 or len(p.rollback) > 300 or len(p.gate_reason) > 160 or len(p.preconditions) > 8:
            bad.append(f"phase 길이 한도 초과: {p.id}")
        if len(p.docs) > MAX_DOCS_PER_PHASE:
            bad.append(f"phase docs는 {MAX_DOCS_PER_PHASE}개까지: {p.id}")
        for grp in p.expect:
            for pat in grp:
                try:
                    re.compile(pat)
                except re.error:
                    bad.append(f"expect 정규식 오류: {p.id}:{pat}")
    if len(sc.investigation) > MAX_INVESTIGATION:
        bad.append(f"조사 명령은 {MAX_INVESTIGATION}개까지")
    if len(sc.summary) > 300:
        bad.append("summary는 300자까지")
    used = {n for t in (*sc.investigation, sc.summary, *(p.guidance for p in sc.phases)) for n in _TEMPLATE_PARAM.findall(t)}
    if used - set(sc.params):
        bad.append("정의하지 않은 param 사용: " + ",".join(sorted(used - set(sc.params))))
    for name, spec in sc.params.items():
        if not spec.placeholder and not spec.patterns:
            bad.append(f"param 값을 뽑는 방법이 없다: {name}")
        for pat in spec.patterns:
            try:
                re.compile(pat)
            except re.error:
                bad.append(f"param 정규식 오류: {name}")
    for cmd in sc.investigation:
        if not cmd.startswith(("aws ", "kubectl ")):
            bad.append(f"조사 명령은 aws·kubectl만: {cmd[:30]}")
    return bad


# ---------------- 매칭 ----------------
def match_scenario(scenarios: list[Scenario], masked_text: str, services: tuple[str, ...] = ()) -> ScenarioMatch | None:
    """마스킹된 요청이 시나리오에 맞는지 서버 규칙으로 판정한다. 필수 param이 하나라도 없으면 맞지 않는 것으로 본다(일반 계획으로 간다)."""
    text = masked_text.lower()
    for sc in scenarios:
        if sc.services_any and not set(sc.services_any) & set(services):
            continue
        if any(not any(k in text for k in grp) for grp in sc.all_of) or any(k in text for k in sc.none_of):
            continue
        params: dict[str, str] = {}
        for name, spec in sc.params.items():
            val = None
            if spec.placeholder:
                m = re.search(PLACEHOLDER.format(cat=re.escape(spec.placeholder)), masked_text)
                val = m.group(0) if m else None
            else:
                for pat in spec.patterns:
                    m = re.search(pat, masked_text)
                    if m:
                        val = m.group(1)
                        break
            if val:
                params[name] = val
        if any(spec.required and name not in params for name, spec in sc.params.items()):
            continue
        return ScenarioMatch(sc, params)
    return None


def missing_expectations(phase: ScenarioPhase, steps) -> list[str]:
    """phase의 필수 항목(`expect`) 중 step에서 찾지 못한 것의 번호(`expect:<phase>:<n>`). step은 `kind`·`title`·`command`를 가진 객체."""
    hay = [f"{getattr(s, 'kind', '')} {getattr(s, 'title', '')} {getattr(s, 'command', '') or ''}".lower() for s in steps]
    return [f"scenario_missing:{phase.id}:{i + 1}" for i, grp in enumerate(phase.expect)
            if not any(re.search(pat, h, re.IGNORECASE) for pat in grp for h in hay)]
