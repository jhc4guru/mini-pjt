"""검색 방식 비교 평가: BM25 / 벡터 / 하이브리드(RRF) × 용어 사전 확장 × (선택) LLM 리랭크.

실행: .venv\\Scripts\\python.exe eval\\eval_retrieval.py [--no-vector] [--rerank] [--show-failures N]
- 임베딩과 리랭크는 Bedrock을 호출한다. 질의는 마스킹된 형태(플레이스홀더)만 쓴다.
- 결과는 eval/reports/retrieval_<시각>.json 에 저장된다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.rag.chunks import load_chunks  # noqa: E402
from app.rag.retriever import HybridRetriever  # noqa: E402

KS = (1, 3, 5)


def load_cases() -> list[dict]:
    data = yaml.safe_load((ROOT / "eval" / "retrieval_cases.yaml").read_text(encoding="utf-8"))
    return data["cases"]


def score(ids: list[str], expect: list[str]) -> tuple[dict[int, bool], float]:
    rank = next((i for i, cid in enumerate(ids, 1) if cid in expect), None)
    hit = {k: rank is not None and rank <= k for k in KS}
    return hit, (1.0 / rank if rank else 0.0)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-vector", action="store_true", help="Bedrock 호출 없이 BM25만 평가")
    ap.add_argument("--rerank", action="store_true", help="LLM 리랭크 변형도 평가")
    ap.add_argument("--show-failures", type=int, default=0, help="마지막 변형의 실패 케이스를 N개 출력")
    args = ap.parse_args()

    config.load_env()
    chunks = load_chunks()
    ids = {c.id for c in chunks}
    cases = load_cases()
    bad = [(c["id"], e) for c in cases for e in c["expect"] if e not in ids]
    if bad:
        print("ERROR 평가셋의 정답 id가 문서에 없음:", bad)
        return 1

    vector = reranker = None
    if not args.no_vector:
        from app.rag.embedder import TitanEmbedder
        from app.rag.vector_index import VectorIndex

        t0 = time.perf_counter()
        vector = VectorIndex(TitanEmbedder())
        print("색인 동기화:", vector.sync(chunks), f"({time.perf_counter() - t0:.1f}s)")
    if args.rerank:
        from app.rag.reranker import LLMReranker

        reranker = LLMReranker()
    retr = HybridRetriever(chunks, vector_index=vector, reranker=reranker)

    exp_modes = [("", False), ("+대상", "entities"), ("+동작", "actions"), ("+전체", "all")]
    variants = [(f"bm25{label}", dict(method="bm25", expand=mode)) for label, mode in exp_modes]
    if vector:
        for method in ("vector", "hybrid"):
            variants += [(f"{method}{label}", dict(method=method, expand=mode)) for label, mode in exp_modes]
        if reranker:
            variants.append(("hybrid+대상+rerank", dict(method="hybrid", expand="entities", rerank=True)))
            variants.append(("hybrid+rerank", dict(method="hybrid", expand=False, rerank=True)))

    report: dict = {"time": datetime.now().isoformat(timespec="seconds"), "cases": len(cases), "variants": {}}
    last_failures: list[str] = []
    header = f"{'변형':<22}" + "".join(f"hit@{k:<4}" for k in KS) + f"{'MRR':<8}{'ms/질의':<8}"
    print("\n" + header + "\n" + "-" * len(header))
    for name, kw in variants:
        agg = {k: 0 for k in KS}
        mrr = 0.0
        by_tag: dict[str, list[bool]] = defaultdict(list)
        failures = []
        t0 = time.perf_counter()
        for c in cases:
            res = retr.search(c["q"], k=5, **kw)
            got = [h.chunk.id for h in res.hits]
            hit, rr = score(got, c["expect"])
            for k in KS:
                agg[k] += hit[k]
            mrr += rr
            for t in c.get("tags", []):
                by_tag[t].append(hit[3])
            if not hit[3]:
                failures.append(f"{c['id']} {c['q']!r} 기대={c['expect'][0]} 상위3={got[:3]}")
        ms = (time.perf_counter() - t0) * 1000 / len(cases)
        n = len(cases)
        print(f"{name:<22}" + "".join(f"{agg[k] / n:<9.2f}" for k in KS) + f"{mrr / n:<8.3f}{ms:<8.0f}")
        report["variants"][name] = {
            **{f"hit@{k}": round(agg[k] / n, 4) for k in KS}, "mrr": round(mrr / n, 4), "ms_per_query": round(ms),
            "hit@3_by_tag": {t: round(sum(v) / len(v), 3) for t, v in by_tag.items()},
            "failures_hit@3": failures,
        }
        last_failures = failures

    print("\n태그별 hit@3 (마지막 변형 기준):", report["variants"][variants[-1][0]]["hit@3_by_tag"])
    if args.show_failures and last_failures:
        print(f"\n[{variants[-1][0]}] hit@3 실패 {len(last_failures)}건 (상위 {args.show_failures}건):")
        for f in last_failures[: args.show_failures]:
            print("  -", f)

    out = ROOT / "eval" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"retrieval_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("리포트:", path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
