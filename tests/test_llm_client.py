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
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from app.llm import ChatResult
    c, _ = make({"m-main": ["ok"]})
    spec = {"toolSpec": {"name": "t", "description": "d", "inputSchema": {"json": {"type": "object"}}}}
    r = c.chat("MAIN", [HumanMessage(content="x")], system="sys", tools=[spec])
    assert isinstance(r, ChatResult) and r.text == "ok" and r.tool_calls == []
    leaky = [HumanMessage(content="x"), AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1"}]),
             ToolMessage(content='{"output": "sg-0123456789abcdef0"}', tool_call_id="1")]
    with pytest.raises(LeakError):
        c.chat("MAIN", leaky)
    leaky_use = [HumanMessage(content="x"), AIMessage(content="", tool_calls=[{"name": "t", "args": {"q": "sg-0123456789abcdef0"}, "id": "1"}])]
    with pytest.raises(LeakError):
        c.chat("MAIN", leaky_use)


def test_a_non_throttling_error_stops_without_fallback_and_records_only_its_kind(tmp_path):
    audit = AuditLog(tmp_path / "a.sqlite")

    class ReadTimeoutError(Exception):                      # 응답 코드가 없는 전송 오류(긴 응답이 읽기 제한에 걸린 경우)
        pass
    c, _ = make({"m-main": [ReadTimeoutError("Read timeout on endpoint URL: https://bedrock-runtime.us-east-1.amazonaws.com 123456789012")]}, audit=audit)
    with pytest.raises(LLMUnavailable) as ex:
        c.complete("MAIN", "x", session_id="s2")
    assert "ReadTimeoutError" in str(ex.value) and "123456789012" not in str(ex.value) and c._client.calls == ["m-main"]       # 폴백하지 않고 원인 종류만 올린다
    recs = [r["reasons"][0] for r in audit.query(session_id=None) if r["reasons"] and r["reasons"][0].startswith("llm_error")]
    assert recs and recs[0].startswith("llm_error:MAIN:ReadTimeoutError") and "123456789012" not in recs[0]


# ---- 프롬프트 캐싱(D-54) ----
class CacheBedrock:
    """받은 요청을 기록하고 캐시 토큰이 든 응답을 돌려준다. `reject`면 첫 호출을 캐시 지점 거부로 실패시킨다."""

    def __init__(self, reject=False, reject_message="The model does not support cachePoint blocks"):
        self.calls, self.reject, self.reject_message = [], reject, reject_message

    def converse(self, **kw):
        import copy
        self.calls.append(copy.deepcopy(kw))
        if self.reject and "cachePoint" in str(kw):
            e = Exception(self.reject_message)
            e.response = {"Error": {"Code": "ValidationException"}}
            raise e
        return {"output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}}, "stopReason": "end_turn",
                "usage": {"inputTokens": 20, "outputTokens": 5, "cacheReadInputTokens": 700, "cacheWriteInputTokens": 30}}


SPEC = {"toolSpec": {"name": "t", "description": "d", "inputSchema": {"json": {"type": "object"}}}}


def make_cache(client, *, prompt_cache=True, audit=None):
    return LLMClient(Masker(Inventory.from_file(FIXTURE)), client=client, prompt_cache=prompt_cache, audit=audit)


def _has_cache_point(call) -> bool:
    return "cachePoint" in str(call)


def test_prompt_cache_is_off_by_default_and_for_single_shot_calls():
    from langchain_core.messages import HumanMessage
    fake = CacheBedrock()
    LLMClient(Masker(Inventory.from_file(FIXTURE)), client=fake).chat("MAIN", [HumanMessage(content="x")], system="s", tools=[SPEC], cache=True)
    assert not _has_cache_point(fake.calls[0])                           # 클라이언트가 꺼져 있으면 cache=True여도 쓰지 않는다
    fake2 = CacheBedrock()
    c = make_cache(fake2)
    c.complete("MAIN", "x", system="s")
    c.chat("MAIN", [HumanMessage(content="x")], system="s", tools=[SPEC])                       # cache=False(기본)
    assert not any(_has_cache_point(call) for call in fake2.calls)


def test_prompt_cache_marks_system_tools_and_conversation_and_reports_cache_tokens():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    fake = CacheBedrock()
    c = make_cache(fake)
    msgs = [HumanMessage(content="q"), AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1"}]), ToolMessage(content="{}", tool_call_id="1")]
    r = c.chat("MAIN", msgs, system="sys", tools=[SPEC], cache=True)
    call = fake.calls[0]
    assert any("cachePoint" in b for b in call["system"]) and any("cachePoint" in t for t in call["toolConfig"]["tools"])
    assert any("cachePoint" in b for m in call["messages"] for b in m["content"])
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens) == (750, 5, 700, 30)       # 입력은 읽기·쓰기를 포함한 전체


def test_the_leak_check_runs_before_a_cached_call():
    from langchain_core.messages import HumanMessage
    fake = CacheBedrock()
    with pytest.raises(LeakError):
        make_cache(fake).chat("MAIN", [HumanMessage(content="대상은 sg-0123456789abcdef0 이다")], system="s", tools=[SPEC], cache=True)
    assert fake.calls == []


def test_a_rejected_cache_point_falls_back_to_a_plain_request_once_and_is_audited(tmp_path):
    from langchain_core.messages import HumanMessage
    audit = AuditLog(tmp_path / "a.sqlite")
    fake = CacheBedrock(reject=True)
    c = make_cache(fake, audit=audit)
    r = c.chat("MAIN", [HumanMessage(content="x")], system="s", tools=[SPEC], cache=True)
    assert r.text == "ok" and [_has_cache_point(x) for x in fake.calls] == [True, False]       # 거부된 요청 뒤에 캐시 없이 한 번 다시 보냄
    c.chat("MAIN", [HumanMessage(content="y")], system="s", tools=[SPEC], cache=True)
    assert [_has_cache_point(x) for x in fake.calls] == [True, False, False]                    # 이후에는 캐시를 시도하지 않는다
    assert any(r["reasons"] == ["prompt_cache_rejected:MAIN"] for r in audit.query(session_id=None))


def test_a_validation_error_unrelated_to_cache_still_stops_without_retry():
    from langchain_core.messages import HumanMessage
    fake = CacheBedrock(reject=True, reject_message="Malformed input request, please reformat your input")
    with pytest.raises(LLMUnavailable):
        make_cache(fake).chat("MAIN", [HumanMessage(content="x")], system="s", tools=[SPEC], cache=True)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("extra", [{}, {"cacheDetails": [{"ttl": "5m", "inputTokens": 30}]}])
def test_cache_write_tokens_are_counted_with_or_without_ttl_details(extra):
    """TTL별 상세가 있으면 langchain-aws가 쓰기 토큰을 `cache_creation`에서 `ephemeral_5m_input_tokens`로 옮긴다. 어느 쪽이든 30으로 집계한다."""
    from langchain_core.messages import HumanMessage

    class Fake(CacheBedrock):
        def converse(self, **kw):
            r = super().converse(**kw)
            r["usage"].update(extra)
            return r

    r = make_cache(Fake()).chat("MAIN", [HumanMessage(content="x")], system="s", tools=[SPEC], cache=True)
    assert (r.cache_read_tokens, r.cache_write_tokens) == (700, 30)
