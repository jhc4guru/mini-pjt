# rerank_demo.py - 재랭킹 전후 순위 비교
from sentence_transformers import CrossEncoder
from hybrid import hybrid

# 다국어 재랭킹 모델 (한국어 지원, 최초 실행 시 다운로드)
reranker = CrossEncoder("BAAI/bge-reranker-v2-m3")

query = "연차 이월 규정"
candidates = [d.page_content for d in hybrid.invoke(query)]  # 1차 검색 후보

scores = reranker.predict([(query, c) for c in candidates])
ranked = sorted(zip(scores, candidates), reverse=True)
for s, c in ranked[:3]:
    print(f"{s:.3f}  {c[:50]}...")