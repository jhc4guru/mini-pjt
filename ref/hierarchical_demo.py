# hierarchical_demo.py - 조회팀과 분석팀을 지휘하는 계층 구조
from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langchain.agents import create_agent
from langgraph_supervisor import create_supervisor

load_dotenv()
MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

def llm():
    return ChatBedrockConverse(model=MODEL, region_name="us-east-1", temperature=0)

# 교육용 더미 도구입니다
@tool
def retrieve_docs(query: str) -> str:
    """사내 규정 문서를 검색한다."""
    return f"[문서] {query}: 장비 사외 반출 시 팀장 승인 필수"

@tool
def search_db(target: str) -> str:
    """사내 데이터를 조회한다."""
    return f"[데이터] {target}: 노트북 2대, GPU 서버 1대"

@tool
def analyze(data: str) -> str:
    """데이터를 통계 분석한다."""
    return f"[분석] {data} 결과: 자산 집중도 높음, 분산 배치 권장"

@tool
def write_report(content: str) -> str:
    """마크다운 보고서를 작성한다."""
    return f"# 보고서\n\n{content}"

# 1) 조회팀: 문서 검색과 데이터 조회를 조회팀 Lead가 지휘
research_agent = create_agent(model=llm(), tools=[retrieve_docs],
                                    system_prompt="문서 검색 전담.", name="research_agent")
data_agent = create_agent(model=llm(), tools=[search_db],
                                system_prompt="사내 데이터 조회 전담.", name="data_agent")

research_team = create_supervisor(
    [research_agent, data_agent],
    model=llm(),
    prompt="너는 조회팀 Lead다. 문서 검색과 데이터 조회 작업을 분배하라.",
).compile(name="research_team")   # 팀 그래프는 compile 시 name이 필수입니다

# 2) 분석팀: 분석과 보고서를 분석팀 Lead가 지휘
analyst_agent = create_agent(model=llm(), tools=[analyze],
                                   system_prompt="통계 분석 전담.", name="analyst_agent")
report_agent = create_agent(model=llm(), tools=[write_report],
                                  system_prompt="보고서 작성 전담.", name="report_agent")

analysis_team = create_supervisor(
    [analyst_agent, report_agent],
    model=llm(),
    prompt="너는 분석팀 Lead다. 분석과 보고서 작업을 분배하라.",
).compile(name="analysis_team")

# 3) 최상위: 두 팀을 상위 관리자가 지휘 (팀 그래프를 Agent처럼 넣습니다)
top = create_supervisor(
    [research_team, analysis_team],
    model=llm(),
    prompt=(
        "너는 회사 전체 Supervisor다.\n"
        "- 데이터, 문서 조회는 research_team\n"
        "- 분석, 보고서 작성은 analysis_team\n"
        "조회가 먼저 필요하면 research_team부터 호출하라."
    ),
).compile()

if __name__ == "__main__":
    q = "클라우드운영팀 GPU 자산을 조회해서 분석하고 보고서로 만들어줘"
    print(f"질문: {q}\n")
    for event in top.stream(
        {"messages": [HumanMessage(content=q)]},
        stream_mode="updates",
        subgraphs=True,        # 팀 내부 흐름까지 추적합니다
        config={"recursion_limit": 40},
    ):
        path, update = event   # subgraphs=True면 경로와 업데이트 튜플로 옵니다
        for node in update:
            print(f"  {'/'.join(map(str, path)) or 'top'} -> {node}")