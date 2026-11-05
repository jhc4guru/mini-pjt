"""마스킹과 복원.

LLM·임베딩·리랭크·트레이스 같은 외부 경계로 나가는 텍스트는 mask()를 거쳐 플레이스홀더만 보이게 한다.
실제 값과의 매핑은 Masker(세션 하나) 안에만 있고 어디에도 기록하지 않는다. 승인 화면과 실행 단계에서만 unmask()로 복원한다.

- 정규식으로 찾는 값: ARN, 리소스 ID, 액세스 키, 계정 ID, 이메일, IP/CIDR (0.0.0.0/0은 공개 상수라 마스킹하지 않음)
- 정규식으로 못 찾는 값: 리소스 이름(버킷, 클러스터, 역할 등) → 인벤토리(알려진 이름 목록)로 치환
- AWS 관리형 정책 ARN(arn:aws:iam::aws:policy/...)은 공개 값이라 그대로 둔다(모델이 정책 이름을 알아야 하므로).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.guards.patterns import SENSITIVE_PATTERNS

PLACEHOLDER_RE = re.compile(r"<([A-Z][A-Z_]*)_(\d+)>")
# 매핑 없이 쓰는 고정 자리표시. 사용자가 로컬에서 채우는 값이며 복원 대상이 아니다.
LITERAL_PLACEHOLDERS = frozenset({"<PASSWORD>"})
# 민감하지 않은 일반 이름은 인벤토리에 있어도 마스킹하지 않는다(문장이 깨지고 변별력이 없다).
NEVER_MASK = frozenset({"default", "kube-system", "kube-public", "kube-node-lease"})
PROD_MARKER = re.compile(r"(?i)(?<![a-z])(?:prod|prd|production)(?![a-z])")

_ID_TYPES = {
    "sg": "SG", "sgr": "SGR", "i": "INSTANCE", "vpc": "VPC", "subnet": "SUBNET", "ami": "AMI", "eni": "ENI",
    "rtb": "RTB", "igw": "IGW", "vol": "VOLUME", "snap": "SNAPSHOT", "nat": "NAT", "eipalloc": "EIP",
    "lt": "LT", "acl": "ACL", "eks": "EKS", "asg": "ASG",
}
_NAME_CATEGORIES = {"CLUSTER", "NODEGROUP", "ASG", "BUCKET", "ROLE", "USER", "POLICY", "NAMESPACE", "LT", "NAME"}
# 겹칠 때 먼저 이기는 종류(작을수록 우선). ARN이 그 안의 계정 ID·이름을 통째로 삼킨다.
_PRIORITY = {"arn": 0, "resource_id": 1, "access_key": 2, "email": 3, "ip_or_cidr": 4, "account_id": 5, "inventory": 6}


@dataclass
class Inventory:
    """알려진 리소스 이름. 카테고리별 목록과 prod 표시."""

    names: dict[str, list[str]] = field(default_factory=dict)
    prod: frozenset[str] = frozenset()

    @classmethod
    def empty(cls) -> "Inventory":
        return cls()

    @classmethod
    def from_file(cls, path: Path | str) -> "Inventory":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        names: dict[str, list[str]] = {}
        for cat, items in (data.get("names") or {}).items():
            cat = str(cat).upper()
            if cat not in _NAME_CATEGORIES:
                raise ValueError(f"인벤토리 카테고리 오류: {cat} (허용: {sorted(_NAME_CATEGORIES)})")
            names[cat] = [str(x) for x in (items or [])]
        return cls(names=names, prod=frozenset(str(x) for x in (data.get("prod") or [])))

    @classmethod
    def from_env(cls) -> "Inventory":
        """INVENTORY_FILE(또는 data/inventory.yaml)에서 읽는다.

        환경변수로 경로를 명시했는데 파일이 없으면 오류다(이름이 마스킹되지 않은 채 나가는 것을 막는다).
        기본 위치에 파일이 없으면 빈 인벤토리다. 이 경우 정규식으로 못 찾는 리소스 이름은 마스킹되지 않는다.
        """
        from app.config import inventory_path

        path, explicit = inventory_path()
        if path.exists():
            return cls.from_file(path)
        if explicit:
            raise FileNotFoundError(f"INVENTORY_FILE로 지정한 인벤토리 파일이 없습니다: {path}")
        return cls.empty()

    def entries(self) -> list[tuple[str, str]]:
        """(이름, 카테고리), 긴 이름 먼저(부분 일치보다 긴 이름이 이기도록)."""
        out = [(n, cat) for cat, items in self.names.items() for n in items if n and n not in NEVER_MASK]
        return sorted(out, key=lambda x: len(x[0]), reverse=True)

    def is_prod(self, name: str) -> bool:
        return name in self.prod or bool(PROD_MARKER.search(name))


@dataclass
class MaskReport:
    counts: dict[str, int] = field(default_factory=dict)  # 종류별 치환 횟수(값은 담지 않는다)


class Masker:
    """세션 하나의 마스킹 상태. 같은 실제 값은 세션 안에서 항상 같은 플레이스홀더가 된다."""

    def __init__(self, inventory: Inventory | None = None):
        self.inventory = inventory or Inventory.empty()
        self._fwd: dict[str, str] = {}
        self._rev: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        entries = self.inventory.entries()
        self._name_cat = {n: c for n, c in entries}
        self._name_re = (
            re.compile(r"(?<![A-Za-z0-9_-])(?:" + "|".join(re.escape(n) for n, _ in entries) + r")(?![A-Za-z0-9_-])")
            if entries
            else None
        )

    # ---- 상태 저장·복원(서버 재시작 후 이어가기, D-29) ----
    def export_state(self) -> dict:
        """세션 매핑(실제 값 포함). 반드시 암호화해서 저장해야 한다(`mapping_store`). 로그·트레이스에 넣지 않는다."""
        return {"fwd": dict(self._fwd), "counters": dict(self._counters)}

    def load_state(self, state: dict) -> None:
        fwd, counters = dict(state["fwd"]), {k: int(v) for k, v in state["counters"].items()}
        for real, ph in fwd.items():
            if not (isinstance(real, str) and isinstance(ph, str) and PLACEHOLDER_RE.fullmatch(ph)):
                raise ValueError("매핑 상태 형식 오류")
        self._fwd, self._counters = fwd, counters
        self._rev = {ph: real for real, ph in fwd.items()}

    # ---- 내부 ----
    def _placeholder(self, ptype: str, real: str) -> str:
        if real in self._fwd:
            return self._fwd[real]
        n = self._counters.get(ptype, 0) + 1
        self._counters[ptype] = n
        ph = f"<{ptype}_{n}>"
        self._fwd[real] = ph
        self._rev[ph] = real
        return ph

    @staticmethod
    def _regex_type(kind: str, value: str) -> str:
        if kind == "arn":
            return "ARN"
        if kind == "resource_id":
            parts = value.split("-")
            return _ID_TYPES.get(parts[0], "ID") if len(parts) == 2 else "ID"      # 접두사가 여러 토막(vpc-cidr-assoc-…)이면 종류를 단정하지 않는다
        if kind == "access_key":
            return "ACCESS_KEY"
        if kind == "account_id":
            return "ACCOUNT"
        if kind == "email":
            return "EMAIL"
        return "CIDR" if "/" in value else "IP"

    def _candidates(self, text: str) -> list[tuple[int, int, str, str]]:
        cands: list[tuple[int, int, str, str]] = []
        for kind, pat in SENSITIVE_PATTERNS.items():
            for m in pat.finditer(text):
                cands.append((m.start(), m.end(), kind, self._regex_type(kind, m.group(0))))
        if self._name_re:
            for m in self._name_re.finditer(text):
                cands.append((m.start(), m.end(), "inventory", self._name_cat[m.group(0)]))
        return cands

    # ---- 공개 API ----
    def mask_with_report(self, text: str) -> tuple[str, MaskReport]:
        cands = self._candidates(text)
        # 앞에서부터, 같은 위치면 우선순위·긴 것 먼저. 이미 덮인 구간과 겹치는 후보는 버린다.
        cands.sort(key=lambda c: (c[0], _PRIORITY[c[2]], -(c[1] - c[0])))
        out, pos, report = [], 0, MaskReport()
        for start, end, kind, ptype in cands:
            if start < pos:
                continue
            out.append(text[pos:start])
            out.append(self._placeholder(ptype, text[start:end]))
            report.counts[ptype] = report.counts.get(ptype, 0) + 1
            pos = end
        out.append(text[pos:])
        return "".join(out), report

    def mask(self, text: str) -> str:
        return self.mask_with_report(text)[0]

    def unmask(self, text: str) -> str:
        """플레이스홀더를 실제 값으로 복원한다. 매핑에 없는 플레이스홀더는 그대로 남긴다."""
        return PLACEHOLDER_RE.sub(lambda m: self._rev.get(m.group(0), m.group(0)), text)

    def unresolved(self, text: str) -> list[str]:
        """복원되지 않고 남은 플레이스홀더(고정 자리표시 제외). 모델이 지어낸 플레이스홀더를 잡는다."""
        return [
            m.group(0)
            for m in PLACEHOLDER_RE.finditer(text)
            if m.group(0) not in self._rev and m.group(0) not in LITERAL_PLACEHOLDERS
        ]

    def leaked_kinds(self, text: str) -> list[str]:
        """마스킹이 끝났어야 할 텍스트에 남은 민감값의 종류. 값은 돌려주지 않는다.

        패턴에 걸리는 값 + 이 세션이 아는 실제 값(인벤토리 이름 포함)이 그대로 있으면 누출이다.
        """
        kinds = {kind for kind, pat in SENSITIVE_PATTERNS.items() if pat.search(text)}
        for real in self._fwd:
            if real in text:
                kinds.add("known_value")
                break
        return sorted(kinds)

    def prod_targets(self, restored_text: str) -> list[str]:
        """복원된 텍스트에 나온 인벤토리 이름 중 prod로 보이는 것의 개수 표시용 카테고리(이름은 돌려주지 않는다)."""
        if not self._name_re:
            return []
        return sorted({self._name_cat[m.group(0)] for m in self._name_re.finditer(restored_text) if self.inventory.is_prod(m.group(0))})
