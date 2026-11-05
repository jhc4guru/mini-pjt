"""검색의 임베딩·리랭크(langchain-aws)와 단발 호출 그래프(`run_structured`)의 오프라인 테스트(D-53)."""
import io
import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.guards.agent_limits import SchemaViolation
from app.guards.boundary import LeakError
from app.planner.structured import run_structured
from app.rag.embedder import EmbeddingError, TitanEmbedder
from app.rag.reranker import LLMReranker


class FakeRuntime:
    """boto3 bedrock-runtime 클라이언트 흉내: invoke_model(임베딩)과 converse(리랭크)."""
    meta = SimpleNamespace(region_name="us-east-1")

    def __init__(self, *, embed=None, converse_text=None, fail=False):
        self.bodies, self.converse_calls, self.embed, self.converse_text, self.fail = [], [], embed or [0.6, 0.8], converse_text, fail

    def invoke_model(self, **kw):
        if self.fail:
            raise RuntimeError("AccessDenied 123456789012 " + "x" * 300)
        self.bodies.append(json.loads(kw["body"]))
        return {"body": io.BytesIO(json.dumps({"embedding": self.embed}).encode())}

    def converse(self, **kw):
        self.converse_calls.append(kw)
        if self.fail:
            raise RuntimeError("boom")
        return {"output": {"message": {"role": "assistant", "content": [{"text": self.converse_text}]}}, "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1}}


def test_embedder_uses_langchain_bedrock_embeddings_with_titan_v2_params_and_caches(monkeypatch):
    monkeypatch.setenv("EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    rt = FakeRuntime()
    e = TitanEmbedder(client=rt)
    assert e.embed("보안 그룹") == pytest.approx([0.6, 0.8]) and e.embed("보안 그룹") == pytest.approx([0.6, 0.8])
    assert len(rt.bodies) == 1                                         # 같은 텍스트는 캐시
    assert rt.bodies[0]["dimensions"] == 1024                          # normalize는 Titan v2 기본값(true)에 맡기고 LangChain이 한 번 더 정규화한다
    assert [pytest.approx([0.6, 0.8]) == v for v in e.embed_many(["a", "b"])] == [True, True]


def test_embedding_errors_are_short_and_do_not_leak_the_message_tail(monkeypatch):
    monkeypatch.setenv("EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    e = TitanEmbedder(client=FakeRuntime(fail=True))
    with pytest.raises(EmbeddingError) as ex:
        e.embed("x")
    assert len(str(ex.value)) <= 120 and "x" * 100 not in str(ex.value)


class Hit:
    def __init__(self, i):
        self.chunk = SimpleNamespace(id=i, text=f"doc {i}")


def test_reranker_reorders_with_chat_bedrock_converse_and_is_fail_open(monkeypatch):
    monkeypatch.setenv("READ_MODEL", "m-read")
    hits = [Hit("a"), Hit("b"), Hit("c")]
    rt = FakeRuntime(converse_text='{"ranking": ["c", "a", "zzz"]}')
    r = LLMReranker(client=rt)
    assert [h.chunk.id for h in r.rerank("질의", hits)] == ["c", "a", "b"]       # 모르는 id는 무시, 빠진 후보는 뒤에 그대로
    assert rt.converse_calls[0]["modelId"] == "m-read" and rt.converse_calls[0]["inferenceConfig"]["temperature"] == 0
    assert [h.chunk.id for h in LLMReranker(client=FakeRuntime(fail=True)).rerank("질의", hits)] == ["a", "b", "c"]       # 실패하면 원래 순서
    assert LLMReranker(client=rt).rerank("질의", hits[:1]) == hits[:1]


class Step(BaseModel):
    cmd: str
    risk: str


def test_run_structured_graph_retries_then_returns_the_valid_value():
    outs = iter(['{"cmd": 1}', "not json", '{"cmd": "x", "risk": "low"}'])
    calls = []
    assert run_structured(lambda: calls.append(1) or next(outs), Step, max_retries=2).cmd == "x" and len(calls) == 3


def test_run_structured_graph_escalates_after_the_retry_limit():
    calls = []
    with pytest.raises(SchemaViolation):
        run_structured(lambda: calls.append(1) or '{"cmd": 1}', Step, max_retries=2)
    assert len(calls) == 3


def test_run_structured_graph_lets_call_exceptions_through_without_retry():
    calls = []

    def boom():
        calls.append(1)
        raise LeakError("llm:test", ["resource_id"])

    with pytest.raises(LeakError):
        run_structured(boom, Step)
    assert len(calls) == 1
