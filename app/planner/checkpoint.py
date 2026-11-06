"""계획 생성의 체크포인트(D-68). 한도·모델 오류로 중단됐을 때 이미 끝낸 부분(조사 결과, phase 윤곽, 완료한 phase)을 남겨
'다시 시도'가 처음부터가 아니라 이어서 진행하게 한다.

- 담는 것은 모두 마스킹된 값이다(윤곽·phase 초안은 모델 출력이고 서버가 이미 검증했으며, 읽기 결과는 정리·요약된 것이다).
  불러올 때 누출 검사와 전체 재검증(`build_plan`, 명령 가드)을 다시 하고, 하나라도 어긋나면 체크포인트를 버리고 처음부터 한다(fail-closed).
- 요청 키(마스킹된 요청 + iac 설정)가 같을 때만 쓴다. 사용자가 내용을 바꾸면 키가 달라져 자동으로 버려진다.
- 체크포인트는 승인·실행과 무관하다. plan이 되려면 이어서 끝까지 만든 뒤 평소처럼 `build_plan` 전체 검증과 사람의 승인을 거친다.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

MAX_DOCS = 120
MAX_READS = 40
MAX_READ_CHARS = 600


def request_key(masked_text: str, iac_managed: bool) -> str:
    return hashlib.sha256(f"{int(bool(iac_managed))}\n{masked_text}".encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class PlanCheckpoint:
    key: str
    outline: dict | None = None                                  # 수락된 윤곽(`_OutlineInput` 덤프). 없으면 조사 단계에서 멈춘 것
    phases: tuple[dict, ...] = ()                                # 수락된 phase(`PhaseDraft` 덤프), 윤곽 순서의 앞부분
    docs: dict[str, tuple[str, str]] = field(default_factory=dict)   # 찾은 문서 명령 id → (명령, 종류)
    reads: dict[str, str] = field(default_factory=dict)          # 마스킹된 읽기 명령 → 정리·요약된 결과

    @property
    def progress(self) -> tuple[int, int]:
        """(완료한 phase 수, 전체 phase 수). 윤곽 전이면 (0, 0)."""
        return len(self.phases), len((self.outline or {}).get("phases", []))

    @property
    def has_progress(self) -> bool:
        return bool(self.outline or self.docs or self.reads)

    def to_json(self) -> str:
        return json.dumps({"key": self.key, "outline": self.outline, "phases": list(self.phases),
                           "docs": {k: list(v) for k, v in list(self.docs.items())[-MAX_DOCS:]},
                           "reads": dict(list(self.reads.items())[-MAX_READS:])}, ensure_ascii=False)

    @staticmethod
    def from_json(raw: str | None) -> "PlanCheckpoint | None":
        if not raw:
            return None
        try:
            d = json.loads(raw)
            docs = {str(k): (str(v[0]), str(v[1])) for k, v in dict(d.get("docs") or {}).items()}
            reads = {str(k): str(v)[:MAX_READ_CHARS] for k, v in dict(d.get("reads") or {}).items()}
            outline = d.get("outline")
            phases = tuple(d.get("phases") or ())
            if not isinstance(d.get("key"), str) or (outline is not None and not isinstance(outline, dict)) or not all(isinstance(p, dict) for p in phases):
                return None
            return PlanCheckpoint(d["key"], outline, phases, docs, reads)
        except (ValueError, TypeError, IndexError, KeyError, AttributeError):
            return None
