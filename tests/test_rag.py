"""검색기 오프라인 테스트(Bedrock 호출 없음). 실행: python -m pytest tests -q"""
import pytest

from app.guards.patterns import find_sensitive
from app.rag.chunks import load_chunks
from app.rag.glossary import Glossary
from app.rag.retriever import HybridRetriever, SensitiveQueryError, rrf_fuse
from app.rag.tokenizer import tokenize


@pytest.fixture(scope="module")
def chunks():
    return load_chunks()


@pytest.fixture(scope="module")
def retriever(chunks):
    return HybridRetriever(chunks, vector_index=None)


# ---- chunks ----
def test_chunks_cover_all_commands(chunks):
    assert len(chunks) >= 140
    assert len({c.id for c in chunks}) == len(chunks)


def test_chunk_text_has_command_kind_and_impact(chunks):
    c = next(c for c in chunks if c.id == "aws:eks:update-cluster-version")
    assert "aws eks update-cluster-version" in c.text
    assert "되돌릴 수 없음" in c.text
    assert c.kind == "mutate" and c.risk == "high"


def test_manual_commands_are_marked(chunks):
    c = next(c for c in chunks if c.id == "kubectl:kubectl:exec")
    assert c.kind == "manual" and "서버는 실행하지 않음" in c.text


# ---- tokenizer ----
def test_tokenizer_keeps_compound_and_parts():
    t = tokenize("create-security-group 만들어줘")
    assert "create-security-group" in t and {"create", "security", "group"} <= set(t)


def test_tokenizer_options_and_placeholders():
    t = tokenize("--group-id <SG_1> 인바운드")
    assert "--group-id" in t and "group" in t and "인바운드" in t
    assert not any("sg" == x or x.startswith("sg_") for x in t)


# ---- glossary ----
def test_glossary_expands_synonyms():
    g = Glossary()
    e = g.expand("시큐리티그룹 인바운드 열어줘")
    assert {"security", "group", "ingress", "authorize"} <= set(e.terms)


def test_glossary_short_alias_needs_word_boundary():
    g = Glossary()
    assert "sg" not in g.expand("msg 보내줘").matched
    assert "sg" in g.expand("sg 열어줘").matched


def test_glossary_ambiguous_is_reported_not_resolved():
    e = Glossary().expand("방화벽 열어줘")
    assert any(a["alias"] == "방화벽" and "Security Group" in a["candidates"] for a in e.ambiguous)


def test_server_and_node_mean_ec2_and_are_not_ambiguous():
    g = Glossary()
    for q in ("서버 목록 조회", "노드 재시작", "워커 노드 종료"):
        e = g.expand(q)
        assert not any(a["alias"] in ("서버", "노드") for a in e.ambiguous), q
        assert "ec2" in e.terms, q
    assert "pod" in g.expand("파드 재시작").terms and "ec2" not in g.expand("파드 재시작").terms


# ---- 민감값 패턴 ----
@pytest.mark.parametrize("text", [
    "sg-0123456789abcdef0 열어줘", "123456789012 계정", "arn:aws:iam::123456789012:role/x",
    "AKIAABCDEFGHIJKLMNOP", "10.0.0.0/16 허용", "admin@example.com",
])
def test_sensitive_detected(text):
    assert find_sensitive(text)


@pytest.mark.parametrize("text", ["<SG_1> 인바운드 443 열어줘", "0.0.0.0/0 전체 개방", "EKS 1.36 업그레이드", "포트 8080:80"])
def test_sensitive_not_flagged(text):
    assert not find_sensitive(text)


def test_retriever_refuses_unmasked_query_without_echoing_value(retriever):
    with pytest.raises(SensitiveQueryError) as e:
        retriever.search("sg-0123456789abcdef0 인바운드 열어줘", method="bm25")
    assert "sg-0123456789abcdef0" not in str(e.value)


class _ExplodingIndex:
    """호출되면 실패하는 벡터 색인. 거부가 외부 모델 호출보다 먼저 일어나는지 확인한다."""

    def query(self, *args, **kwargs):
        raise AssertionError("마스킹되지 않은 질의로 외부 모델 호출이 일어남")


def test_refusal_happens_before_any_external_call(chunks):
    r = HybridRetriever(chunks, vector_index=_ExplodingIndex())
    with pytest.raises(SensitiveQueryError):
        r.search("123456789012 계정에서 열어줘", method="hybrid")


# ---- 결합 / 검색 ----
def test_rrf_prefers_items_ranked_by_both():
    s = rrf_fuse({"a": ["x", "y", "z"], "b": ["y", "x", "w"]})
    assert max(s, key=s.get) in {"x", "y"} and s["w"] < s["x"]


def test_bm25_exact_command_name_is_top1(retriever):
    r = retriever.search("authorize-security-group-ingress", method="bm25", expand=False)
    assert r.hits[0].chunk.id == "aws:ec2:authorize-security-group-ingress"


def test_bm25_korean_query_with_glossary_finds_command(retriever):
    r = retriever.search("시큐리티그룹 인바운드 포트 열어줘", method="bm25", k=5)
    assert "aws:ec2:authorize-security-group-ingress" in [h.chunk.id for h in r.hits]


def test_filters(retriever):
    r = retriever.search("삭제", method="bm25", k=10, cli="kubectl", kinds={"mutate"})
    assert r.hits and all(h.chunk.cli == "kubectl" and h.chunk.kind == "mutate" for h in r.hits)
