"""knowledge/cli 문서를 명령 단위 chunk로 만든다.

chunk 하나 = 명령 하나. 텍스트에는 문서 제목, 실행 형태, 종류·위험도·영향 요약, 본문 섹션이 들어간다.
메타데이터(cli, service, kind 등)는 필터와 승인 화면, 출력 가드가 쓴다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import KNOWLEDGE_DIR

KIND_KO = {"read": "조회(읽기 전용)", "mutate": "변경", "manual": "수동 실행(서버는 실행하지 않음)", "restricted": "금지"}
DISRUPTION_KO = {"none": "없음", "possible": "가능", "likely": "높음"}
REVERSIBLE_KO = {"full": "되돌릴 수 있음", "partial": "일부만 되돌릴 수 있음", "irreversible": "되돌릴 수 없음"}

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)
_HEADING = re.compile(r"^## (.+?)\s*$", re.M)
_TITLE = re.compile(r"^# (.+?)\s*$", re.M)


@dataclass(frozen=True)
class Chunk:
    id: str                      # "aws:ec2:create-security-group"
    cli: str                     # aws | kubectl
    service: str                 # ec2, eks, s3, s3api, iam, autoscaling, ssm, kubectl
    name: str                    # create-security-group, "rollout status"
    topic: str
    kind: str                    # read | mutate | manual | restricted
    risk: str
    guard: tuple[str, ...]
    impact: dict = field(default_factory=dict, compare=False, hash=False)
    path: str = ""
    text: str = ""               # 색인용 텍스트
    required: tuple[str, ...] = field(default=(), compare=False)    # 문서의 필수 옵션(명령 만들기 도우미·초안 검증에 쓴다)
    optional: tuple[str, ...] = field(default=(), compare=False)

    @property
    def command(self) -> str:
        return f"aws {self.service} {self.name}" if self.cli == "aws" else f"kubectl {self.name}"

    @property
    def meta(self) -> dict:
        """벡터 저장소용 평탄한 메타데이터(스칼라만)."""
        return {
            "cli": self.cli, "service": self.service, "name": self.name, "topic": self.topic,
            "kind": self.kind, "risk": self.risk, "guard": ",".join(self.guard), "path": self.path,
        }


def _impact_text(kind: str, impact: dict) -> str:
    if kind not in ("mutate", "manual") or not impact:
        return ""
    parts = [
        f"서비스 중단 가능성 {DISRUPTION_KO.get(impact.get('disruption'), '불명')}",
        REVERSIBLE_KO.get(impact.get("reversible"), "되돌림 불명"),
    ]
    if impact.get("replaces"):
        parts.append("삭제 후 재생성")
    if impact.get("iac"):
        parts.append("IaC(Terraform)로 관리될 수 있음")
    if impact.get("long_running"):
        parts.append("오래 걸림")
    return "영향: " + ", ".join(parts)


def _split_sections(body: str) -> tuple[str, str, dict[str, str]]:
    title_m = _TITLE.search(body)
    title = title_m.group(1) if title_m else ""
    heads = list(_HEADING.finditer(body))
    sections: dict[str, str] = {}
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        sections[h.group(1)] = body[h.end():end].strip()
    return title, body[: heads[0].start()] if heads else body, sections


def load_chunks(knowledge_dir: Path | None = None) -> list[Chunk]:
    root = Path(knowledge_dir or KNOWLEDGE_DIR)
    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*.md")):
        if path.name == "SCHEMA.md":
            continue
        raw = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        m = _FRONTMATTER.match(raw)
        if not m:
            continue
        meta = yaml.safe_load(m.group(1))
        title, _intro, sections = _split_sections(m.group(2))
        rel = path.relative_to(root.parent.parent).as_posix() if root.parent.parent in path.parents else path.as_posix()
        for c in meta["commands"]:
            name = c["name"]
            cid = f"{meta['cli']}:{meta['service']}:{name}"
            cmd = f"aws {meta['service']} {name}" if meta["cli"] == "aws" else f"kubectl {name}"
            lines = [
                title,
                cmd,
                f"종류: {KIND_KO.get(c['kind'], c['kind'])} / 위험도: {c['risk']}",
            ]
            imp = _impact_text(c["kind"], c.get("impact") or {})
            if imp:
                lines.append(imp)
            if c.get("guard"):
                lines.append("가드: " + ", ".join(c["guard"]))
            lines.append(sections.get(name, ""))
            chunks.append(
                Chunk(
                    id=cid, cli=meta["cli"], service=meta["service"], name=name, topic=meta["topic"],
                    kind=c["kind"], risk=c["risk"], guard=tuple(c.get("guard") or ()),
                    impact=c.get("impact") or {}, path=rel, text="\n".join(x for x in lines if x),
                    required=tuple(str(x) for x in (c.get("required") or ())), optional=tuple(str(x) for x in (c.get("optional") or ())),
                )
            )
    return chunks
