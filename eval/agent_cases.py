"""에이전트 평가셋(eval/agent_cases.csv) 로더.

열: id, category, input, expected_traits, forbidden, expected_tools, note (UTF-8, 스프레드시트에서 편집 가능).
expected_traits, forbidden, expected_tools는 세미콜론(;)으로 항목을 구분한다.
"""
from __future__ import annotations

import csv
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent / "agent_cases.csv"
COLUMNS = ("id", "category", "input", "expected_traits", "forbidden", "expected_tools", "note")
CATEGORIES = ("positive", "negative", "edge", "guardrail")


def split_items(value: str) -> list[str]:
    return [x.strip() for x in (value or "").split(";") if x.strip()]


def load_cases(path: Path | None = None) -> list[dict]:
    with Path(path or CSV_PATH).open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return [
        {
            "id": int(r["id"]),
            "category": r["category"],
            "input": r["input"],
            "expected_traits": split_items(r["expected_traits"]),
            "forbidden": split_items(r["forbidden"]),
            "expected_tools": split_items(r["expected_tools"]),
            "note": r.get("note", ""),
        }
        for r in rows
    ]


def judge_view_of_input(text: str, masker) -> str:
    """judge·외부 모델에 보낼 입력 표현. 마스킹된 텍스트만 쓰고, 입력 가드가 막은 입력은 원문 대신 사유 코드만 쓴다."""
    from app.guards.input_guard import check_user_input

    v = check_user_input(text, masker)
    if not v.allow:
        codes = sorted({str(r).split(":")[0] for r in v.reasons}) or ["blocked"]
        return f"[입력 차단됨: {', '.join(codes)}]"
    return v.masked_text
