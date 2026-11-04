# rag_chain.py - 기본 RAG 체인
from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse, BedrockEmbeddings
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough

load_dotenv()

llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
    temperature=0,
)
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

prompt = ChatPromptTemplate.from_messages([
    ("system",
     "너는 삼성SDS 사내 정책 안내 어시스턴트야. 아래 사내 문서만 참고해 답해. "
     "문서에 없는 내용은 반드시 '사내 문서에서 찾을 수 없습니다'라고 답해. "
     "답변 끝에 참고한 문서를 '(출처: 파일명)' 형식으로 붙여.\n\n"
     "--- 사내 문서 ---\n{context}"),
    ("human", "{question}"),
])


def format_docs(docs):
    return "\n\n---\n\n".join(
        f"[{d.metadata.get('source', '?')}] {d.page_content}" for d in docs
    )


rag_chain = (
    {"context": retriever | format_docs, "question": RunnablePassthrough()}
    | prompt
    | llm
    | StrOutputParser()
)

if __name__ == "__main__":
    print(rag_chain.invoke("클라우드운영팀 김하늘입니다. 연차 이월은 며칠까지 되나요?"))