# specialist_agents.py - 서브 Agent 3종 정의와 단독 테스트
from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langchain.agents import create_agent

load_dotenv()
MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
worker_llm = ChatBedrockConverse(model=MODEL, region_name="us-east-1", temperature=0)

# ---------- 도구 (지금은 더미, 4번째 세션에서 실제 RAG와 MCP로 교체) ----------
# 아래는 모두 교육용 더미 데이터이며, 도구 이름과 인자를 4일차 MCP 서버와 똑같이 맞춰 두었습니다
# (4번째 세션에서 진짜 도구로 바꿔도 이름과 인자가 같으므로 조회 결과가 달라지지 않습니다)

@tool
def search_employee(name: str) -> str:
    """임직원 이름으로 소속 팀과 직무를 조회한다. 개인 정보 조회에 사용한다."""
    data = {
        "김하늘": "김하늘 / 클라우드운영팀 / 팀장",
        "박도윤": "박도윤 / 물류플랫폼팀 / 백엔드",
        "이서준": "이서준 / 클라우드운영팀 / 인프라 엔지니어",
        "최민준": "최민준 / 물류플랫폼팀 / 프론트엔드",
        "정하은": "정하은 / 클라우드운영팀 / 데이터 분석가",
    }
    return data.get(name, f"'{name}' 직원을 찾을 수 없습니다.")

@tool
def list_team_members(team: str) -> str:
    """팀 이름으로 소속 임직원 목록을 조회한다. 팀 인원수나 명단이 필요할 때 사용한다."""
    data = {
        "클라우드운영팀": "클라우드운영팀 3명: 김하늘(팀장), 이서준(인프라 엔지니어), 정하은(데이터 분석가)",
        "물류플랫폼팀": "물류플랫폼팀 2명: 박도윤(백엔드), 최민준(프론트엔드)",
    }
    return data.get(team, f"'{team}' 팀을 찾을 수 없습니다.")

@tool
def get_asset(owner: str = "") -> str:
    """사용자 이름으로 보유 자산을 조회한다. 빈 문자열이면 전체 자산을 반환한다."""
    data = {
        "김하늘": "김하늘 자산 2건: 노트북(갤럭시북4 프로), 모니터(뷰피니티 S8)",
        "박도윤": "박도윤 자산 1건: 노트북(갤럭시북4 프로)",
        "이서준": "이서준 자산 2건: 노트북(갤럭시북4), GPU 서버(NVIDIA A100 80GB)",
        "최민준": "최민준 자산 1건: 노트북(갤럭시북4 프로)",
        "정하은": "정하은 자산 1건: 모니터(뷰피니티 S8)",
    }
    if not owner:
        return "전체 자산 8건: 노트북 5대, 모니터 2대, GPU 서버 1대"
    return data.get(owner, f"'{owner}' 사용자의 자산이 없습니다.")

@tool
def list_projects(team: str) -> str:
    """팀 이름으로 진행 중인 프로젝트 목록을 조회한다."""
    data = {
        "클라우드운영팀": "클라우드운영팀 프로젝트: 클라우드 비용 최적화, 사내 AI 어시스턴트 도입",
        "물류플랫폼팀": "물류플랫폼팀 프로젝트: 차세대 물류 플랫폼 구축, 물류 관제 대시보드 고도화",
    }
    return data.get(team, f"'{team}' 팀의 프로젝트가 없습니다.")

@tool
def retrieve_docs(query: str) -> str:
    """사내 규정 문서에서 관련 내용을 검색한다."""
    # 2일차에 만든 사내 정책 문서 3종의 실제 수치를 그대로 씁니다
    docs = {
        "연차": "연차 규정: 연 15일 부여, 입사 1년 미만은 매월 1일씩 최대 11일, 미사용 연차는 5일까지 이월",
        "재택": "재택근무 규정: 주 2일까지 가능, 팀 필수 출근일(수요일) 제외, 전주 목요일까지 인사 포털 신청(RW-201)",
        "출장": "출장 규정: 국내 식비 하루 5만원, 국내 숙박 1박 12만원, 출발 3영업일 전까지 신청(TR-102)",
    }
    for key, value in docs.items():
        if key in query:
            return value
    return "관련 문서를 찾지 못했습니다."

# ---------- 서브 Agent 3종 ----------

data_agent = create_agent(
    worker_llm,
    [search_employee, list_team_members, get_asset, list_projects],
    system_prompt=(
        "너는 삼성SDS 사내 데이터 조회 전문가다. 임직원, 자산, 프로젝트 조회만 담당한다.\n"
        "개인은 search_employee, 팀 명단과 인원수는 list_team_members를 쓴다.\n"
        "반드시 도구로 조회한 결과에 근거해 답하고, 조회 결과를 명확한 한 문단으로 정리해 보고하라.\n"
        "담당 범위 밖 질문에는 '담당 범위가 아닙니다'라고만 답하라."
    ),
    name="data_agent",
)

research_agent = create_agent(
    worker_llm,
    [retrieve_docs],
    system_prompt=(
        "너는 삼성SDS 사내 규정 문서 검색 전문가다. 연차, 재택근무, 출장 등 규정 검색만 담당한다.\n"
        "반드시 retrieve_docs 결과에 근거해서만 답하고 근거 문장을 함께 인용하라.\n"
        "담당 범위 밖 질문에는 '담당 범위가 아닙니다'라고만 답하라."
    ),
    name="research_agent",
)

general_agent = create_agent(
    worker_llm,
    [],
    system_prompt=(
        "너는 삼성SDS 사내 어시스턴트의 일반 응대 담당이다.\n"
        "인사말과 잡담에 친절히 답하고, 업무 범위 밖 질문에는\n"
        "'사내 데이터 조회와 규정 검색을 도와드릴 수 있습니다'라고 안내하라."
    ),
    name="general_agent",
)

# ---------- 단독 테스트: 조립 전에 각 Agent를 따로 검증 ----------

def get_text(message):
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환합니다."""
    content = message.content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content

def solo_test(agent, question: str):
    result = agent.invoke({"messages": [HumanMessage(question)]})
    print(f"[{agent.name}] Q: {question}")
    print(f"  A: {get_text(result['messages'][-1])[:150]}\n")

if __name__ == "__main__":
    solo_test(data_agent, "클라우드운영팀 임직원 명단 알려줘")
    solo_test(data_agent, "연차 규정 알려줘")        # 범위 밖 거절 확인
    solo_test(research_agent, "출장 숙박비 한도 얼마야?")
    solo_test(general_agent, "안녕! 뭘 할 수 있어?")