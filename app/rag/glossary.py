"""용어 사전: 사용자 표현(sg, 시큐리티그룹, 방화벽 …)을 표준 영어 용어로 바꿔 질의를 확장한다.

LLM 없이 규칙으로만 동작한다(빠르고 결정적). 모호한 표현은 되묻기 후보로만 알려 주고 임의로 고르지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import KNOWLEDGE_DIR

_ASCII_ONLY = re.compile(r"^[\x00-\x7f]+$")
_PAREN = re.compile(r"\([^)]*\)")


@dataclass
class Expansion:
    original: str
    terms: list[str] = field(default_factory=list)          # 덧붙일 표준 영어 용어
    matched: list[str] = field(default_factory=list)        # 일치한 사용자 표현
    ambiguous: list[dict] = field(default_factory=list)     # 되묻기 후보 [{alias, candidates, note}]

    @property
    def expanded(self) -> str:
        return (self.original + " " + " ".join(self.terms)).strip() if self.terms else self.original


def _alias_regex(alias: str) -> re.Pattern[str]:
    a = re.escape(alias.lower())
    if _ASCII_ONLY.match(alias):  # 짧은 영문 약어(sg, ns, vm)는 단어 경계로만 일치시킨다
        return re.compile(rf"(?<![a-z0-9]){a}(?![a-z0-9])")
    return re.compile(a)


def _canon_terms(canonical: str, cli: str = "") -> list[str]:
    """표준 용어에서 덧붙일 영어 단어를 뽑는다. 괄호 안 단어도 포함하고, CLI는 서비스 토큰만 덧붙인다."""
    outside = _PAREN.sub(" ", canonical)
    inside = " ".join(re.findall(r"\(([^)]*)\)", canonical))
    terms = [w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", outside + " " + inside)]
    if cli:
        terms.append(cli.split()[-1].lower())  # "aws ec2" -> "ec2" ("aws"는 모든 문서에 있어 제외)
    return list(dict.fromkeys(terms))


class Glossary:
    SECTIONS = ("entities", "actions")

    def __init__(self, path: Path | None = None):
        data = yaml.safe_load(Path(path or KNOWLEDGE_DIR / "glossary.yaml").read_text(encoding="utf-8")) or {}
        # 모호한 표현(서버, 키 등)은 자동 확장하지 않는다. 되묻기 후보로만 알린다.
        ambiguous_aliases = {str(a["alias"]).lower() for a in data.get("ambiguous", []) or [] if a.get("candidates")}
        self._rules: list[tuple[str, re.Pattern[str], str, list[str]]] = []
        for section in self.SECTIONS:
            for item in data.get(section, []) or []:
                terms = _canon_terms(item["canonical"], item.get("cli", "") if section == "entities" else "")
                for alias in item["aliases"]:
                    if str(alias).lower() in ambiguous_aliases:
                        continue
                    self._rules.append((section, _alias_regex(str(alias)), str(alias), terms))
        self._ambiguous = [
            (_alias_regex(a["alias"]), a, [str(x).lower() for x in (a.get("exclude") or [])]) for a in data.get("ambiguous", []) or [] if a.get("candidates")
        ]

    def expand(self, query: str, sections: tuple[str, ...] = SECTIONS) -> Expansion:
        """sections: 확장에 쓸 사전 구역(entities=대상, actions=동작)."""
        q = query.lower()
        exp = Expansion(original=query)
        for section, pat, alias, terms in self._rules:
            if section in sections and pat.search(q):
                exp.matched.append(alias)
                for t in terms:
                    if t not in exp.terms:
                        exp.terms.append(t)
        for pat, item, exclude in self._ambiguous:
            text = q
            for ex in exclude:                                 # 뜻이 분명한 복합어(ssh키, 키 페어 등)에 든 표현은 모호하다고 보지 않는다
                text = text.replace(ex, " ")
            if pat.search(text):
                exp.ambiguous.append({"alias": item["alias"], "candidates": item["candidates"], "note": item.get("note", "")})
        return exp
