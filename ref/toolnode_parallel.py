# toolnode_parallel.py - ToolNode + 병렬 호출 + 에러 처리 확인
import json
from typing import TypedDict, Annotated, Literal
from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage

load_dotenv()


def get_text(message):
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환합니다."""
    content = message.content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content


@tool
def get_temperature(city: str) -> str:
    """도시의 현재 온도를 조회한다 (교육용 더미 데이터)."""
    data = {"서울": "3도", "부산": "8도", "제주": "11도"}
    if city not in data:
        raise ValueError(f"{city}는 지원하지 않는 도시입니다. 지원: {list(data)}")
    return f"{city}: {data[city]}"


@tool
def divide(a: float, b: float) -> float:
    """두 수를 나눈다."""
    return a / b     # b가 0이면 ZeroDivisionError, ToolNode가 처리


tools = [get_temperature, divide]


class State(TypedDict):
    messages: Annotated[list, add_messages]


llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-east-1",
).bind_tools(tools)


def agent(state: State) -> dict:
    msgs = [SystemMessage(content="도구를 사용해 답하세요.")] + state["messages"]
    return {"messages": [llm.invoke(msgs)]}


def should_continue(state: State) -> Literal["tools", "end"]:
    return "tools" if state["messages"][-1].tool_calls else "end"


builder = StateGraph(State)
builder.add_node("agent", agent)
builder.add_node("tools", ToolNode(tools, handle_tool_errors=True))
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", should_continue, {"tools": "tools", "end": END})
builder.add_edge("tools", "agent")
graph = builder.compile()


if __name__ == "__main__":
    # 1) 병렬 호출: 한 질문에 도구 두 개가 한 턴에 호출됨
    print("=== 병렬 호출 ===")
    result = graph.invoke({"messages": [HumanMessage(content="서울과 부산의 온도를 동시에 알려주세요.")]})
    #print(result)
    #print(json.dumps(result, ensure_ascii=False, indent=2,
    #             default=lambda o: o.model_dump()))
    for m in result["messages"]:
        if getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                print(f"  도구 호출 요청: {tc['name']}({tc['args']})")
        elif m.type == "tool":
            print(f"  도구 결과: {get_text(m)}")
    print(f"최종 답변: {get_text(result['messages'][-1])}")

    # 2) 에러 처리: 0으로 나누기 -> ToolMessage로 변환되어 Agent가 복구
    print("\n=== 에러 처리 (handle_tool_errors=True) ===")
    result = graph.invoke({"messages": [HumanMessage(content="10 나누기 0은?")]})
    print(f"최종 답변: {get_text(result['messages'][-1])}")
    