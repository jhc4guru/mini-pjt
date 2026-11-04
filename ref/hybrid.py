# hybrid.py - 하이브리드 검색 (BM25 구성은 사전 검증된 완성 코드)
from dotenv import load_dotenv
from langchain_community.retrievers import BM25Retriever
from langchain_classic.retrievers import EnsembleRetriever
from kiwipiepy import Kiwi
from metadata import load_chunks
from rag_chain import db

load_dotenv()
kiwi = Kiwi()


def korean_tokenizer(text: str) -> list[str]:
    """한국어 형태소 단위 토크나이저 (사전 검증본). 명사, 어간, 숫자, 외래어만 남깁니다."""
    keep = {"NNG", "NNP", "NNB", "NR", "NP", "VV", "VA", "SL", "SN"}
    return [t.form for t in kiwi.tokenize(text) if t.tag in keep]


# 1) BM25 검색기 - 벡터 DB가 아니라 청크 리스트에서 직접 만듭니다
bm25 = BM25Retriever.from_documents(load_chunks(), preprocess_func=korean_tokenizer)
bm25.k = 3

# 2) 벡터 검색기
vector = db.as_retriever(search_kwargs={"k": 3})

# 3) 하이브리드 - 가중치 조합 [BM25 비중, 벡터 비중]
hybrid = EnsembleRetriever(retrievers=[bm25, vector], weights=[0.3, 0.7])

queries = [
    "월차 며칠 받아?",        # 의미형 질문 -> 벡터가 유리
    "TR-102 양식 규정",       # 키워드형 질문 -> BM25가 유리
]
for q in queries:
    print("=" * 55)
    print(f"질문: {q}")
    for name, r in [("BM25만", bm25), ("벡터만", vector), ("하이브리드", hybrid)]:
        hits = r.invoke(q)
        srcs = [f'{h.metadata["source"].split("/")[-1]} | {h.page_content[:14].replace(chr(10), " ")}' for h in hits[:3]]
        print(f"  {name}: {srcs}")

    # hybrid_weights.py - 가중치 조정 실험 (hybrid.py에 이어서)
    for w in [0.2, 0.5, 0.8]:
        h = EnsembleRetriever(retrievers=[bm25, vector], weights=[w, 1 - w])
        top = h.invoke("월차 며칠 받아?")[0].metadata["source"].split("/")[-1]
        print(f"BM25 {w:.1f} / 벡터 {1 - w:.1f} -> 1위: {top}")