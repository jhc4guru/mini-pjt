"""하이브리드 검색기: BM25(한국어+CLI 토큰) + 벡터(Titan) → RRF 결합 → (선택) LLM 리랭크.

입력은 반드시 마스킹된 질의여야 한다. 마스킹되지 않은 민감값이 보이면 검색을 거부한다(fail-closed).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rank_bm25 import BM25Okapi

from app.guards.patterns import find_sensitive
from app.rag.chunks import Chunk
from app.rag.glossary import Expansion, Glossary
from app.rag.tokenizer import tokenize

METHODS = ("bm25", "vector", "hybrid")


class SensitiveQueryError(ValueError):
    """마스킹되지 않은 민감값이 질의에 있음."""


@dataclass
class Hit:
    chunk: Chunk
    score: float
    ranks: dict[str, int] = field(default_factory=dict)  # 방법별 순위(1부터)


@dataclass
class SearchResult:
    hits: list[Hit]
    expansion: Expansion
    method: str
    reranked: bool = False

    @property
    def ambiguous(self) -> list[dict]:
        return self.expansion.ambiguous


def rrf_fuse(rankings: dict[str, list[str]], weights: dict[str, float] | None = None, k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion. 점수 = Σ weight / (k + rank). 점수 스케일이 다른 BM25·벡터를 안전하게 합친다."""
    scores: dict[str, float] = {}
    for name, ids in rankings.items():
        w = (weights or {}).get(name, 1.0)
        for rank, cid in enumerate(ids, start=1):
            scores[cid] = scores.get(cid, 0.0) + w / (k + rank)
    return scores


class HybridRetriever:
    def __init__(self, chunks: list[Chunk], vector_index=None, glossary: Glossary | None = None, reranker=None):
        self.chunks = chunks
        self._by_id = {c.id: c for c in chunks}
        self._ids = [c.id for c in chunks]
        self._bm25 = BM25Okapi([tokenize(c.text) for c in chunks])
        self.vector = vector_index
        self.glossary = glossary or Glossary()
        self.reranker = reranker

    # ---- 개별 검색기 ----
    def _bm25_rank(self, query: str) -> list[str]:
        scores = self._bm25.get_scores(tokenize(query))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [self._ids[i] for i in order if scores[i] > 0]

    def _vector_rank(self, query: str, n: int) -> list[str]:
        if self.vector is None:
            raise RuntimeError("벡터 색인이 없습니다(vector_index=None).")
        return [cid for cid, _ in self.vector.query(query, n=n)]

    # ---- 검색 ----
    def search(
        self,
        masked_query: str,
        *,
        k: int = 5,
        method: str = "hybrid",
        expand: bool | str = False,  # 기본 확장 안 함(평가 결과). True/"all"=대상+동작, "entities", "actions"
        rerank: bool | None = None,  # None이면 리랭커가 설정돼 있을 때만 수행

        cli: str | None = None,
        service: str | None = None,
        kinds: set[str] | None = None,
        pool: int = 20,
        weights: dict[str, float] | None = None,
    ) -> SearchResult:
        if method not in METHODS:
            raise ValueError(f"method는 {METHODS} 중 하나")
        bad = find_sensitive(masked_query)
        if bad:  # 값은 출력하지 않고 종류만 알린다
            raise SensitiveQueryError("마스킹되지 않은 민감값 감지: " + ", ".join(sorted({kind for kind, _ in bad})))

        sections = {True: Glossary.SECTIONS, "all": Glossary.SECTIONS, "entities": ("entities",), "actions": ("actions",)}.get(expand)
        exp = self.glossary.expand(masked_query, sections) if sections else self.glossary.expand(masked_query, ())
        q = exp.expanded if sections else masked_query

        rankings: dict[str, list[str]] = {}
        if method in ("bm25", "hybrid"):
            rankings["bm25"] = self._bm25_rank(q)
        if method in ("vector", "hybrid"):
            rankings["vector"] = self._vector_rank(q, n=len(self.chunks))

        def allowed(cid: str) -> bool:
            c = self._by_id[cid]
            return (
                (cli is None or c.cli == cli)
                and (service is None or c.service == service)
                and (kinds is None or c.kind in kinds)
            )

        do_rerank = (self.reranker is not None) if rerank is None else (rerank and self.reranker is not None)
        rankings = {name: [cid for cid in ids if allowed(cid)] for name, ids in rankings.items()}
        fused = rrf_fuse(rankings, weights)
        order = sorted(fused, key=fused.get, reverse=True)
        hits = [
            Hit(self._by_id[cid], fused[cid], {n: ids.index(cid) + 1 for n, ids in rankings.items() if cid in ids})
            for cid in order[: max(k, pool) if do_rerank else k]
        ]
        reranked = False
        if do_rerank and len(hits) > 1:
            new = self.reranker.rerank(masked_query, hits[:pool])  # 용어 사전 단어를 덧붙이지 않은 원 질의로 판단
            reranked = [h.chunk.id for h in new] != [h.chunk.id for h in hits[:pool]]
            hits = new + hits[pool:]
        return SearchResult(hits=hits[:k], expansion=exp, method=method, reranked=reranked)
