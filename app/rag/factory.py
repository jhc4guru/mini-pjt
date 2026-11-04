"""기본 검색기 구성(평가에서 고른 값). 에이전트 도구와 서버는 이 함수로 검색기를 얻는다.

선택 근거(eval/eval_retrieval.py, 80 케이스): 하이브리드(RRF)가 BM25·벡터 단독보다 낫고,
LLM 리랭크가 hit@1을 0.78 → 0.99로 올렸다. 용어 사전 확장은 효과가 일관되지 않아 기본에서 뺐다.
"""
from __future__ import annotations

from app import config
from app.rag.chunks import load_chunks
from app.rag.embedder import TitanEmbedder
from app.rag.reranker import LLMReranker
from app.rag.retriever import HybridRetriever
from app.rag.vector_index import VectorIndex

DEFAULTS = {"method": "hybrid", "expand": False, "k": 5}  # rerank는 리랭커가 있으면 자동 수행


def build_retriever(with_rerank: bool = True) -> HybridRetriever:
    config.load_env()
    chunks = load_chunks()
    index = VectorIndex(TitanEmbedder())
    index.sync(chunks)  # 문서가 바뀐 chunk만 다시 임베딩
    return HybridRetriever(chunks, vector_index=index, reranker=LLMReranker() if with_rerank else None)
