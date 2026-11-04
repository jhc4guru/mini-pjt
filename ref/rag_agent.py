# rag_agent.py - 2일차 벡터 DB를 research_agent의 실제 도구로 연결
from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse, BedrockEmbeddings
from langchain_chroma import Chroma
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langchain.agents import create_agent

load_dotenv()

# 1) 2일차에서 저장해 둔 벡터 DB를 디스크에서 불러옵니다 (임베딩 재생성 없음)
#    임베딩 모델은 2일차에서 저장할 때 쓴 것과 반드시 같아야 합니다
embeddings = BedrockEmbeddings(
    model_id="amazon.titan-embed-text-v2:0",
    region_name="us-east-1",
)
vectorstore = Chroma(
    persist_directory="./chroma_db",
    embedding_function=embeddings,
    collection_name="sds_policies",
)
retriever = vectorstore.as_retriever(search_kwargs={"k": 3})

def get_text(message):
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환합니다."""
    content = message.content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content

# 2) 검색기를 도구로 감쌉니다
@tool
def retrieve_docs(query: str) -> str:
    """삼성SDS 사내 규정 문서(연차, 재택근무, 출장 등)에서 관련 내용을 검색한다."""
    docs = retriever.invoke(query)
    if not docs:
        return "관련 문서를 찾지 못했습니다."
    return "\n\n".join(
        f"[출처: {d.metadata.get('source', '알 수 없음')}]\n{d.page_content}"
        for d in docs
    )

# 3) 진짜 RAG 도구를 가진 research_agent
worker_llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
    temperature=0,
)
research_agent = create_agent(
    worker_llm,
    [retrieve_docs],
    system_prompt=(
        "너는 삼성SDS 사내 규정 문서 검색 전문가다.\n"
        "반드시 retrieve_docs로 검색한 내용에 근거해서만 답하라.\n"
        "답변에 출처를 함께 표기하고, 검색 결과에 없는 내용은 모른다고 답하라.\n"
        "담당 범위 밖 질문에는 '담당 범위가 아닙니다'라고만 답하라."
    ),
    name="research_agent",
)

if __name__ == "__main__":
    result = research_agent.invoke(
        {"messages": [HumanMessage("출장 숙박비 상한이 얼마야?")]}
    )
    print(get_text(result["messages"][-1]))