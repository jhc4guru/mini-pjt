# hitl_flow.py - HITL 3갈래: 승인 / 수정 / 거절
from typing import TypedDict, Annotated
from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.types import interrupt, Command
from langgraph.checkpoint.sqlite import SqliteSaver

load_dotenv()


@tool
def request_business_trip(destination: str, days: int, budget: int) -> str:
    """출장 신청서를 제출한다. 되돌리기 어려운 작업이므로 사람 승인이 필요하다."""
    return f"출장 신청서 제출 완료: {destination} {days}일, 예산 {budget:,}원 (교육용 더미 처리)"


@tool
def approve_business_trip(trip_id: str) -> str:
    """접수된 출장 신청을 승인 처리한다. 되돌리기 어려운 작업이므로 사람 승인이 필요하다."""
    return f"출장 신청 {trip_id} 승인 완료 (교육용 더미 처리)"


@tool
def search_trip_policy(query: str) -> str:
    """사내 출장 규정을 검색한다. 조회 전용이라 승인이 필요 없다."""
    return "출장 규정: 국내 출장 예산 한도는 1일 400,000원입니다. (교육용 더미 데이터)"


def get_text(message):
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환합니다."""
    content = message.content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content


RISKY_TOOLS = {"request_business_trip", "approve_business_trip"}
TOOLS = [request_business_trip, approve_business_trip, search_trip_policy]
TOOL_MAP = {t.name: t for t in TOOLS}

llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
)
llm_with_tools = llm.bind_tools(TOOLS)


class State(TypedDict):
    messages: Annotated[list, add_messages]


def agent(state: State) -> dict:
    """LLM이 도구 호출을 결정하는 노드"""
    return {"messages": [llm_with_tools.invoke(state["messages"])]}


def human_gate(state: State) -> dict:
    """위험 도구 호출 전에 멈춰 사람의 결정을 받는 노드 (단순화: 위험 도구는 한 번에 하나)"""
    call = state["messages"][-1].tool_calls[0]
    answer = interrupt({
        "tool": call["name"],
        "args": call["args"],
        "question": "승인(approve), 수정(modify), 거절(reject) 중 선택하세요.",
    })
    # answer 예시:
    #   {"decision": "approve"}
    #   {"decision": "modify", "args": {"budget": 500000}}
    #   {"decision": "reject", "reason": "예산 초과"}
    decision = answer.get("decision", "reject")

    if decision == "reject":
        return {"messages": [ToolMessage(
            content=f"사용자가 실행을 거절했습니다. 사유: {answer.get('reason', '없음')}. "
                    "실행하지 말고 대안을 안내하세요.",
            tool_call_id=call["id"],
        )]}

    args = call["args"]
    note = ""
    if decision == "modify":
        # 사람이 준 인자로 덮어씁니다 (부분 수정 허용)
        args = {**args, **answer.get("args", {})}
        # 이 한 줄이 없으면 모델이 사용자의 원래 요청값을 그대로 다시 말합니다.
        # 도구는 고친 값으로 실행되는데 화면에만 예전 값이 뜨는 사고가 납니다.
        note = ("\n(사용자가 승인 단계에서 인자를 수정했습니다. "
                f"최종 실행값은 {args} 입니다. 원래 요청값이 아니라 이 값으로 안내하세요.)")

    result = TOOL_MAP[call["name"]].invoke(args)
    return {"messages": [ToolMessage(content=result + note, tool_call_id=call["id"])]}


def run_tools(state: State) -> dict:
    """안전한 도구는 승인 없이 실행하는 노드"""
    results = []
    for call in state["messages"][-1].tool_calls:
        output = TOOL_MAP[call["name"]].invoke(call["args"])
        results.append(ToolMessage(content=output, tool_call_id=call["id"]))
    return {"messages": results}


def route(state: State) -> str:
    last = state["messages"][-1]
    if not getattr(last, "tool_calls", None):
        return "end"
    if any(c["name"] in RISKY_TOOLS for c in last.tool_calls):
        return "gate"
    return "tools"


builder = StateGraph(State)
builder.add_node("agent", agent)
builder.add_node("gate", human_gate)
builder.add_node("tools", run_tools)
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", route, {"gate": "gate", "tools": "tools", "end": END})
builder.add_edge("gate", "agent")
builder.add_edge("tools", "agent")


def run_scenario(title: str, resume_value: dict):
    """1차 invoke로 멈추고 주어진 응답으로 재개하는 헬퍼"""
    print(f"\n=== {title} ===")
    with SqliteSaver.from_conn_string("checkpoints.sqlite") as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": f"trip_{title}"}}

        graph.invoke({"messages": [HumanMessage(
            "부산 2일 출장을 예산 80만 원으로 신청해줘"
        )]}, config=config)

        state = graph.get_state(config)
        print("  승인 요청:", state.tasks[0].interrupts[0].value["args"])

        result = graph.invoke(Command(resume=resume_value), config=config)
        print("  결과:", get_text(result["messages"][-1]))


if __name__ == "__main__":
    # 시나리오 1: 승인 - 제안 그대로 실행
    run_scenario("approve", {"decision": "approve"})

    # 시나리오 2: 수정 - 예산을 50만 원으로 고쳐서 실행
    run_scenario("modify", {"decision": "modify", "args": {"budget": 500000}})

    # 시나리오 3: 거절 - 실행하지 않고 대안 안내
    # 규정이 1일 40만 원이라 2일 80만 원은 한도 안입니다.
    # 사유를 "예산 초과"로 두면 모델이 규정을 조회해 반박합니다.
    run_scenario("reject", {"decision": "reject", "reason": "분기 예산이 이미 소진되었음"})