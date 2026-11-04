# single_agent_problem.py - 단일 Agent의 도구 혼동 재현
from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langchain.agents import create_agent

load_dotenv()

# 교육용 더미 데이터를 돌려주는 도구 8개입니다
@tool
def retrieve_docs(query: str) -> str:
    """사내 정책 문서를 검색한다."""
    return f"[문서] {query} 관련: 연차 규정, 재택근무 규정, 출장 규정"

@tool
def search_budget_policy(query: str) -> str:
    """예산 관련 정책 문서를 검색한다."""
    return "[문서] 예산 정책: 분기 예산은 전 분기 실적 기준으로 편성"

@tool
def search_employees(team: str) -> str:
    """팀별 임직원 정보를 사내 DB에서 조회한다."""
    return f"[DB] {team} 임직원: 김하늘, 이서준, 정하은"

@tool
def search_expenses(team: str) -> str:
    """팀별 비용 집행 내역을 사내 DB에서 조회한다."""
    return f"[DB] {team} 비용: 물류 최적화 1억 2천(초과), 데이터 파이프라인 2천 8백"

@tool
def search_projects(team: str) -> str:
    """팀별 프로젝트 목록을 사내 DB에서 조회한다."""
    return f"[DB] {team} 프로젝트: 물류 최적화, 데이터 파이프라인"

@tool
def search_assets(team: str) -> str:
    """팀별 자산 목록을 사내 DB에서 조회한다."""
    return f"[DB] {team} 자산: 노트북 2대, 모니터 2대, GPU 서버 1대"

@tool
def write_report(content: str) -> str:
    """마크다운 보고서를 작성한다."""
    return f"# 보고서\n\n{content}"

@tool
def send_message(channel: str, msg: str) -> str:
    """사내 메신저로 메시지를 발송한다."""
    return f"[메신저] {channel}: {msg}"

#ALL_TOOLS = [retrieve_docs, search_budget_policy, search_employees,
#             search_expenses, search_projects, search_assets,
#             write_report, send_message]
ALL_TOOLS = [retrieve_docs, search_projects]

llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
)
fat_agent = create_agent(
    llm,
    ALL_TOOLS,
    system_prompt="너는 삼성SDS 만능 어시스턴트다. 알맞은 도구를 골라 답하라.",
)

question = "클라우드운영팀 물류 최적화 관련 내용 알려줘"
for i in range(5):  # 같은 질문을 5회 반복해 선택이 흔들리는지 봅니다
    result = fat_agent.invoke({"messages": [HumanMessage(question)]})
    tools_used = [
        tc["name"]
        for m in result["messages"]
        if getattr(m, "tool_calls", None)
        for tc in m.tool_calls
    ]
    print(f"{i + 1}회 호출 도구: {tools_used}")