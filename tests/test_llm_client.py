from pathlib import Path

import pytest

from app.guards.agent_limits import AgentBudget, BudgetExceeded, Limits
from app.guards.audit import AuditLog
from app.guards.boundary import LeakError
from app.guards.masking import Inventory, Masker
from app.llm import LLMClient, LLMUnavailable

FIXTURE = Path(__file__).parent / "fixtures" / "inventory.sample.yaml"


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeBedrock:
    """모델별로 미리 정해 둔 응답/오류를 차례로 돌려준다."""

    def __init__(self, plan):
        self.plan, self.calls = plan, []

    def converse(self, **kw):
        self.calls.append(kw["modelId"])
        item = self.plan[kw["modelId"]].pop(0)
        if isinstance(item, Exception):
            raise item
        return {"output": {"message": {"content": [{"text": item}]}}, "usage": {"inputTokens": 10, "outputTokens": 5}}


@pytest.fixture(autouse=True)
def models(monkeypatch):
    for r in ("MAIN", "SUB", "READ"):
        monkeypatch.setenv(f"{r}_MODEL", f"m-{r.lower()}")


def make(plan, **kw):
    sleeps = []
    c = LLMClient(Masker(Inventory.from_file(FIXTURE)), client=FakeBedrock(plan), sleep=sleeps.append, **kw)
    return c, sleeps


def test_main_success_uses_main_without_fallback():
    c, _ = make({"m-main": ["ok"]})
    r = c.complete("MAIN", "계획 만들어줘")
    assert (r.text, r.actual_role, r.fell_back) == ("ok", "MAIN", False)


def test_main_throttling_retries_then_falls_back_to_sub(tmp_path):
    audit = AuditLog(tmp_path / "a.sqlite")
    t = ClientError("ThrottlingException")
    c, sleeps = make({"m-main": [t, t, t], "m-sub": ["from-sub"]}, audit=audit)
    r = c.complete("MAIN", "x", session_id="s1")
    assert r.text == "from-sub" and r.requested_role == "MAIN" and r.actual_role == "SUB" and r.fell_back
    assert c._client.calls == ["m-main"] * 3 + ["m-sub"] and len(sleeps) == 2     # 재시도 2회 뒤 폴백
    assert audit.query(session_id="s1")[0]["reasons"] == ["llm_fallback:MAIN->SUB"]


def test_retry_success_does_not_fall_back():
    c, _ = make({"m-main": [ClientError("ThrottlingException"), "ok"]})
    r = c.complete("MAIN", "x")
    assert r.actual_role == "MAIN"


def test_both_throttled_stops_without_using_read():
    t = ClientError("ThrottlingException")
    c, _ = make({"m-main": [t] * 3, "m-sub": [t] * 3, "m-read": ["never"]})
    with pytest.raises(LLMUnavailable):
        c.complete("MAIN", "x")
    assert "m-read" not in c._client.calls


def test_non_throttling_error_does_not_fall_back_and_message_is_short():
    c, _ = make({"m-main": [ClientError("ValidationException")], "m-sub": ["never"]})
    with pytest.raises(LLMUnavailable) as e:
        c.complete("MAIN", "x")
    assert c._client.calls == ["m-main"] and len(str(e.value)) <= 120


@pytest.mark.parametrize("role", ["SUB", "READ"])
def test_sub_and_read_never_fall_back(role):
    t = ClientError("ThrottlingException")
    c, _ = make({"m-sub": [t] * 3, "m-read": [t] * 3, "m-main": ["never"]})
    with pytest.raises(LLMUnavailable):
        c.complete(role, "x")
    assert "m-main" not in c._client.calls


def test_leaky_prompt_is_blocked_before_any_call_and_not_retried_on_fallback():
    c, _ = make({"m-main": ["never"]})
    with pytest.raises(LeakError):
        c.complete("MAIN", "대상은 sg-0123456789abcdef0 이다")
    assert c._client.calls == []


def test_tokens_are_added_to_the_shared_budget_even_after_fallback():
    t = ClientError("ThrottlingException")
    b = AgentBudget(Limits(max_tokens=20))
    c, _ = make({"m-main": [t] * 3, "m-sub": ["a", "b"]}, budget=b)
    c.complete("MAIN", "x")                       # 15 토큰
    with pytest.raises(BudgetExceeded):
        c.complete("SUB", "y")                    # 합산 30 > 20


def test_unknown_role_is_rejected():
    c, _ = make({})
    with pytest.raises(ValueError):
        c.complete("BIG", "x")


def test_chat_sends_tools_and_blocks_leaks_inside_tool_results():
    from app.llm import ChatResult
    c, _ = make({"m-main": ["ok"]})
    msgs = [{"role": "user", "content": [{"text": "x"}]}]
    r = c.chat("MAIN", msgs, system="sys", tools=[{"toolSpec": {"name": "t", "description": "d", "inputSchema": {"json": {"type": "object"}}}}])
    assert isinstance(r, ChatResult) and r.text == "ok" and r.tool_uses == []
    leaky = [{"role": "user", "content": [{"toolResult": {"toolUseId": "1", "content": [{"json": {"output": "sg-0123456789abcdef0"}}]}}]}]
    with pytest.raises(LeakError):
        c.chat("MAIN", leaky)
    leaky_use = [{"role": "assistant", "content": [{"toolUse": {"toolUseId": "1", "name": "t", "input": {"q": "sg-0123456789abcdef0"}}}]}]
    with pytest.raises(LeakError):
        c.chat("MAIN", leaky_use)
