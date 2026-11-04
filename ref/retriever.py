# retriever.py - Retriever 생성과 k값 조정
from dotenv import load_dotenv
from langchain_aws import BedrockEmbeddings
from langchain_chroma import Chroma

load_dotenv()
embeddings = BedrockEmbeddings(
    model_id="amazon.titan-embed-text-v2:0",
    region_name="us-east-1",
)
db = Chroma(
    collection_name="sds_policies",
    embedding_function=embeddings,
    persist_directory="./chroma_db",
)

retriever = db.as_retriever(search_kwargs={"k": 3})
docs = retriever.invoke("연차 신청은 어떻게 하나요?")
for d in docs:
    print(f"[{d.metadata['source']}] {d.page_content[:50]}...")

# k값에 따른 차이 관찰
for k in [1, 3, 5]:
    r = db.as_retriever(search_kwargs={"k": k})
    hits = r.invoke("연차 이월 규정")
    print(f"k={k}: {[h.metadata['source'] for h in hits]}")

# MMR - 넓은 질문에서 여러 문서를 고르게
mmr = db.as_retriever(search_type="mmr", search_kwargs={"k": 3, "fetch_k": 6})
print("MMR:", [h.metadata["source"] for h in mmr.invoke("회사 규정 전반")])