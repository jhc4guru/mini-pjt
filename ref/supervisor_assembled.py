# supervisor_assembled.py - 서브 Agent 3종과 Supervisor 조립
from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage
from langgraph_supervisor import create_supervisor

# 3-1에서 만든 Agent들을 그대로 가져옵니다 (같은 폴더에 두세요)
from specialist_agents import data_agent, research_agent, general_agent, get_text

load_dotenv()

SUPERVISOR_PROMPT = (
    "너는 삼성SDS 사내 어시스턴트의 작업 분배자(Supervisor)다.\n"
    "\n"
    "[배분 기준]\n"
    "- 임직원, 자산, 프로젝트 등 사내 시스템 데이터 조회는 data_agent\n"
    "- 연차, 재택근무, 출장 등 사내 규정 문서 검색은 research_agent\n"
    "- 인사말, 잡담, 위 두 가지에 해당하지 않는 질문은 general_agent\n"
    "\n"
    "[규칙]\n"
    "- 직접 답을 지어내지 말고 반드시 담당 Agent를 통해 확인하라.\n"
    "- 한 질문에 여러 요구가 섞여 있으면 각 Agent를 순서대로 호출해 모두 처리하라.\n"
    "- 모든 결과가 모이면 하나의 답변으로 종합해 사용자에게 전달하라."
)

supervisor_llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
    temperature=0,
)
supervisor = create_supervisor(
    [data_agent, research_agent, general_agent],
    model=supervisor_llm,
    prompt=SUPERVISOR_PROMPT,
)
app = supervisor.compile()

def run(question: str):
    print(f"질문: {question}")
    called = []                     # 호출된 Agent 순서 기록
    for event in app.stream(
        {"messages": [HumanMessage(content=question)]},
        stream_mode="updates",
        config={"recursion_limit": 25},
    ):
        for node, update in event.items():
            if node != "supervisor" and node not in called:
                called.append(node)
            for m in (update or {}).get("messages", []):
                if getattr(m, "tool_calls", None):
                    for tc in m.tool_calls:
                        print(f"  [{node}] 도구: {tc['name']}({tc.get('args', {})})")
                elif getattr(m, "content", None):
                    label = getattr(m, "name", None) or node
                    print(f"  [{label}] {get_text(m)[:130]}")
    print(f"  => 호출 순서: {called}\n")

if __name__ == "__main__":
    run("클라우드운영팀 자산 목록 알려줘")