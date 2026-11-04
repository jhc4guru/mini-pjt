"""검색기 온라인 테스트(Bedrock 호출). RUN_BEDROCK_TESTS=1 일 때만 실행한다."""
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_BEDROCK_TESTS") != "1", reason="RUN_BEDROCK_TESTS=1 필요")


@pytest.fixture(scope="module")
def retriever():
    from app.rag.factory import build_retriever

    return build_retriever()


@pytest.mark.parametrize("query,expected", [
    ("<SG_1> 인바운드 443 열어줘", "aws:ec2:authorize-security-group-ingress"),
    ("디플로이먼트 재시작", "kubectl:kubectl:rollout restart"),
    ("노드 롤링 교체 시작", "aws:autoscaling:start-instance-refresh"),
    ("파드 안에 쉘 접속", "kubectl:kubectl:exec"),
])
def test_default_pipeline_top1(retriever, query, expected):
    from app.rag.factory import DEFAULTS

    r = retriever.search(query, **DEFAULTS)
    assert r.hits[0].chunk.id == expected


def test_manual_kind_is_retrievable_and_marked(retriever):
    r = retriever.search("액세스 키 발급해줘", k=3)
    assert any(h.chunk.id == "aws:iam:create-access-key" and h.chunk.kind == "manual" for h in r.hits)


def test_ambiguous_term_is_reported(retriever):
    r = retriever.search("방화벽 열어줘", k=3)
    assert any(a["alias"] == "방화벽" for a in r.ambiguous)
