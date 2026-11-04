# reducer.py - add_messages reducer 동작 확인
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_core.messages import HumanMessage, AIMessage


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]


def chat(state: ChatState) -> dict:
    # 새 메시지를 리스트로 반환만 하면 add_messages가 알아서 누적합니다
    return {"messages": [AIMessage(content="안녕하세요, 반갑습니다.")]}


builder = StateGraph(ChatState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)
graph = builder.compile()

# 1차 실행
result = graph.invoke({"messages": [HumanMessage(content="안녕하세요?")]})
print("1차 실행 후 메시지 개수:", len(result["messages"]))

# 2차 실행: 이전 messages를 그대로 이어서 넘기면 계속 누적됩니다
result2 = graph.invoke(
    {"messages": result["messages"] + [HumanMessage(content="잘 지내시나요?")]}
)

#print(result2)
print("2차 실행 후 메시지 개수:", len(result2["messages"]))
for m in result2["messages"]:
    print(f"  [{type(m).__name__}] {m.content}")