# two_agent_supervisor.py - 최소 2-Agent Supervisor로 배분 관찰
from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langchain.agents import create_agent
from langgraph_supervisor import create_supervisor

load_dotenv()
MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

# 더미 도구지만 이름과 인자는 4일차 MCP 서버와 똑같이 맞춰 둡니다
@tool
def list_team_members(team: str) -> str:
    """팀 이름으로 소속 임직원 목록을 조회한다."""
    data = {
        "클라우드운영팀": "클라우드운영팀 3명: 김하늘(팀장), 이서준(인프라 엔지니어), 정하은(데이터 분석가)",
        "물류플랫폼팀": "물류플랫폼팀 2명: 박도윤(백엔드), 최민준(프론트엔드)",
    }
    return data.get(team, f"'{team}' 팀을 찾을 수 없습니다.")

@tool
def retrieve_docs(query: str) -> str:
    """사내 규정 문서를 검색한다."""
    return f"검색 결과 [{query}]: 연차는 연 15일 부여, 입사 1년 미만은 매월 1일씩 최대 11일"

worker_llm = ChatBedrockConverse(model=MODEL, region_name="us-east-1", temperature=0)

def get_text(message):
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환합니다."""
    content = message.content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content

# 서브 Agent에는 name이 필수입니다. 이 이름으로 handoff 도구가 만들어집니다
data_agent = create_agent(
    worker_llm,
    [list_team_members],
    system_prompt="너는 사내 데이터 조회 전문가다. 임직원 데이터 조회만 담당한다.",
    name="data_agent",
)
research_agent = create_agent(
    worker_llm,
    [retrieve_docs],
    system_prompt="너는 사내 규정 문서 검색 전문가다. 문서 검색만 담당한다.",
    name="research_agent",
)

supervisor_llm = ChatBedrockConverse(model=MODEL, region_name="us-east-1", temperature=0)
supervisor = create_supervisor(
    [data_agent, research_agent],
    model=supervisor_llm,
    prompt=(
        "너는 삼성SDS 사내 어시스턴트의 작업 분배자다.\n"
        "- 임직원 등 사내 데이터 조회는 data_agent\n"
        "- 연차 등 규정 문서 검색은 research_agent\n"
        "필요한 Agent를 순서대로 호출하고 모두 끝나면 사용자에게 답하라."
    ),
)
app = supervisor.compile()

def run(question: str):
    print(f"질문: {question}")
    for event in app.stream(
        {"messages": [HumanMessage(content=question)]},
        stream_mode="updates",
        config={"recursion_limit": 25},  # 무한 루프 안전장치
    ):
        for node, update in event.items():
            for m in (update or {}).get("messages", []):
                if getattr(m, "tool_calls", None):
                    for tc in m.tool_calls:
                        print(f"  [{node}] 도구 호출: {tc['name']}({tc.get('args', {})})")
                elif getattr(m, "content", None):
                    label = getattr(m, "name", None) or node
                    print(f"  [{label}] {get_text(m)[:120]}")
    print()

run("클라우드운영팀 임직원 명단 알려줘")
run("연차는 며칠이야?")