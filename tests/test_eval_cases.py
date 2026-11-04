"""에이전트 평가셋(eval/agent_cases.csv)의 형식과 데이터 규칙을 검사한다. 에이전트 실행은 하지 않는다."""
import csv
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

import agent_cases  # noqa: E402
from app.guards.agent_limits import READ_ONLY_TOOLS  # noqa: E402
from app.guards.input_guard import check_user_input  # noqa: E402
from app.guards.masking import Inventory, Masker  # noqa: E402
from app.guards.patterns import find_sensitive  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "inventory.sample.yaml"
# 합성 값(실제 환경의 값이 아님): 마스킹 경로를 실제처럼 태우기 위해 입력에 쓸 수 있다.
SYNTHETIC = {
    "sg-0123456789abcdef0", "sg-0fedcba9876543210", "i-0abc123def4567890", "i-0def456abc1237890",
    "vpc-0aaa1111bbb22222c", "10.20.0.0/16", "123456789012", "admin@example.com",
}
RATIO = {"positive": 0.40, "negative": 0.20, "edge": 0.25, "guardrail": 0.15}


@pytest.fixture(scope="module")
def cases():
    return agent_cases.load_cases()


def test_header_matches_the_agreed_columns():
    with agent_cases.CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        assert tuple(next(csv.reader(f))) == agent_cases.COLUMNS


def test_ids_are_sequential_from_one_and_inputs_are_unique(cases):
    assert [c["id"] for c in cases] == list(range(1, len(cases) + 1))
    assert len({c["input"] for c in cases}) == len(cases)


def test_categories_are_valid_and_follow_the_recommended_ratio(cases):
    assert {c["category"] for c in cases} <= set(agent_cases.CATEGORIES)
    n, counts = len(cases), Counter(c["category"] for c in cases)
    for cat, target in RATIO.items():
        assert abs(counts[cat] / n - target) <= 0.05, f"{cat}: {counts[cat]}/{n}"


def test_required_fields_are_filled(cases):
    for c in cases:
        assert c["input"].strip(), c["id"]
        assert c["expected_traits"], f"{c['id']}: expected_traits 비어 있음"
        if c["category"] in ("negative", "guardrail"):
            assert c["forbidden"], f"{c['id']}: {c['category']} 케이스는 forbidden이 있어야 한다"


def test_expected_tools_are_agent_read_only_tools(cases):
    for c in cases:
        assert set(c["expected_tools"]) <= READ_ONLY_TOOLS, (c["id"], c["expected_tools"])


def test_inputs_contain_only_synthetic_values(cases):
    """입력에는 실제 환경의 값이 없고, 마스킹 시험용 합성 값만 있다."""
    for c in cases:
        for kind, value in find_sensitive(c["input"]):
            assert value in SYNTHETIC, f"{c['id']}: 합성 값 목록에 없는 민감값 패턴 {kind}"


def test_non_guardrail_inputs_pass_our_own_input_guard(cases):
    """정상 요청이 입력 가드에 막히면 평가가 에이전트까지 가지 못한다."""
    masker = Masker(Inventory.from_file(FIXTURE))
    for c in cases:
        if c["category"] != "guardrail":
            v = check_user_input(c["input"], masker)
            assert v.allow, f"{c['id']}: {v.reasons}"


def test_guardrail_cases_that_are_input_attacks_are_blocked_by_the_input_guard(cases):
    masker = Masker(Inventory.from_file(FIXTURE))
    blocked = {c["id"] for c in cases if c["category"] == "guardrail" and not check_user_input(c["input"], masker).allow}
    # 인젝션·승인 우회·프롬프트 노출·비밀값·문서 속 지시는 입력 단계에서, 시크릿 출력·금지 옵션은 명령 가드 단계에서 막는다.
    assert len(blocked) >= 4


def test_all_columns_contain_only_synthetic_values(cases):
    """입력뿐 아니라 expected_traits, forbidden, note에도 합성 값 밖의 민감값 패턴이 없다."""
    for c in cases:
        for col in ("input", "expected_traits", "forbidden", "expected_tools", "note"):
            v = c[col]
            text = "; ".join(v) if isinstance(v, list) else v
            for kind, value in find_sensitive(text):
                assert value in SYNTHETIC, f"{c['id']}.{col}: 합성 값 목록에 없는 민감값 패턴 {kind}"


def test_judge_view_has_no_raw_values_and_replaces_blocked_inputs(cases):
    masker = Masker(Inventory.from_file(FIXTURE))
    blocked_seen = 0
    for c in cases:
        view = agent_cases.judge_view_of_input(c["input"], masker)
        assert not find_sensitive(view), f"{c['id']}: judge 입력에 민감값 패턴"
        if view.startswith("[입력 차단됨:"):
            blocked_seen += 1
            assert c["input"] not in view
    assert blocked_seen >= 4
