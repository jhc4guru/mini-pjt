# react_agent.py (1/3) - 도구 준비
from typing import TypedDict, Annotated, Literal
from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage

load_dotenv()
llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
)


def get_text(message):
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환합니다."""
    content = message.content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content


@tool
def search_employee(name: str) -> str:
    """임직원 이름으로 소속 팀과 이메일을 조회합니다. 임직원 정보 질문에 사용하세요."""
    # 교육용 더미 데이터입니다. 실제 사내 데이터와 무관합니다.
    employees = {
        "김하늘": {"team": "클라우드운영팀", "email": "haneul.kim@samsungsds.example.com"},
        "박도윤": {"team": "물류플랫폼팀", "email": "doyun.park@samsungsds.example.com"},
    }
    if name in employees:
        e = employees[name]
        return f"{name} / {e['team']} / {e['email']}"
    return f"'{name}' 님을 찾을 수 없습니다. 조회 가능한 임직원: {list(employees.keys())}"


@tool
def calculate(expression: str) -> str:
    """수식 문자열을 계산해 정확한 결과를 반환합니다. 예: '3480000 * 1.1', '48 * 1.12'"""
    try:
        return str(eval(expression, {"__builtins__": {}}, {}))
    except Exception as e:
        return f"계산 실패: {e}. 수식 형식을 확인하세요."


@tool
def classify_request(text: str) -> str:
    """사내 문의 문장을 받아 담당 부서를 IT지원, 인사, 총무 중 하나로 분류합니다. 문의 접수와 부서 배정 질문에 사용하세요."""
    # 도구 안에서 다시 LLM을 호출합니다. 도구 안의 AI입니다.
    result = llm.invoke(
        "다음 사내 문의의 담당 부서를 IT지원, 인사, 총무 중 하나로만 답하세요. "
        f"다른 말은 붙이지 마세요.\n\n문의: {text}"
    )
    return get_text(result).strip()


tools = [search_employee, calculate, classify_request]

# react_agent.py (2/3) - 그래프 조립

# [수동 루프의 messages 리스트] -> State + add_messages reducer
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


SYSTEM_PROMPT = """너는 삼성SDS 사내 AI 어시스턴트야.

[도구 사용 전략]
1. 임직원 정보는 반드시 search_employee로 조회해. 지어내지 마.
2. 계산이 필요하면 calculate를 사용해. 암산하지 마.
3. 사내 문의의 담당 부서 판단은 classify_request를 사용해.
4. 도구가 필요 없는 인사나 일반 질문에는 도구 없이 바로 답해."""

llm_with_tools = llm.bind_tools(tools)


# [수동 루프의 invoke와 append] -> agent 노드
def agent(state: AgentState) -> dict:
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
    return {"messages": [llm_with_tools.invoke(messages)]}


# [수동 루프의 if not ai_message.tool_calls] -> 라우팅 함수
def should_continue(state: AgentState) -> Literal["tools", "end"]:
    last = state["messages"][-1]
    return "tools" if last.tool_calls else "end"


builder = StateGraph(AgentState)
builder.add_node("agent", agent)
builder.add_node("tools", ToolNode(tools))   # [도구 실행과 ToolMessage 포장] -> 내장 ToolNode

builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", should_continue, {"tools": "tools", "end": END})
builder.add_edge("tools", "agent")           # [while 루프] -> Cycle

graph = builder.compile()

# 구조 시각화 (PNG 저장이 안 되는 환경 대비 try/except)
try:
    graph.get_graph().draw_mermaid_png(output_file_path="react_agent.png")
    print("react_agent.png 저장 완료")
except Exception as e:
    print("PNG 저장은 건너뜁니다:", e)
    print(graph.get_graph().draw_mermaid())

# react_agent.py (3/3) - 동작 확인
def run_and_trace(question: str):
    print(f"\n{'=' * 60}\n질문: {question}\n{'=' * 60}")
    result = graph.invoke({"messages": [HumanMessage(content=question)]})
    for m in result["messages"]:
        label = type(m).__name__
        if hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                print(f"  [{label}] 도구 요청: {tc['name']}({tc['args']})")
        elif m.content:
            print(f"  [{label}] {get_text(m)[:150]}")


if __name__ == "__main__":
    # 경로 1: 도구 미사용 질문
    run_and_trace("어떤 일을 도와줄 수 있는지 한 문장으로 소개해줘.")

    # 경로 2: 단일 도구 질문 (도구별 1개씩)
    run_and_trace("김하늘 님은 어느 팀 소속이야?")
    run_and_trace("3480000 곱하기 1.1은 얼마야?")
    run_and_trace("'노트북 화면이 계속 깜빡거려요'라는 문의는 어느 부서 담당이야?")

    # 경로 3: 복합 질문 (도구 2개 이상 체이닝)
    run_and_trace(
        "'프린터가 고장 났어요' 문의를 담당 부서로 분류하고, "
        "그 부서의 이번 달 처리 건수 48건이 12% 늘어나면 몇 건이 되는지도 계산해줘."
    )
    run_and_trace("박도윤 님 이메일을 알려주고, '연차 이월 기준이 궁금해요' 문의의 담당 부서도 분류해줘.")