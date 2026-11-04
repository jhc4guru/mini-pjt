# interrupt_basic.py - 그래프를 멈추고 사람을 기다리기 (체크포인터 필수)
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.types import interrupt, Command
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain_core.messages import HumanMessage, AIMessage


class State(TypedDict):
    messages: Annotated[list, add_messages]
    pending_action: str
    decision: str


def get_text(message):
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환합니다."""
    content = message.content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content


def propose_action(state: State) -> dict:
    """사용자가 위험 작업을 요청했다고 가정합니다."""
    return {"pending_action": "출장 신청서 제출: 부산 2일, 예산 800,000원"}


def human_approval(state: State) -> dict:
    """interrupt: 사람 승인을 기다립니다."""
    decision = interrupt({
        "type": "approval",
        "action": state["pending_action"],
        "question": "이 작업을 실행할까요? (yes/no)",
    })
    return {"decision": decision}


def execute(state: State) -> dict:
    if state["decision"] == "yes":
        return {"messages": [AIMessage(content=f"실행됨: {state['pending_action']}")]}
    return {"messages": [AIMessage(content="실행 거부됨")]}


builder = StateGraph(State)
builder.add_node("propose", propose_action)
builder.add_node("approval", human_approval)
builder.add_node("execute", execute)
builder.add_edge(START, "propose")
builder.add_edge("propose", "approval")
builder.add_edge("approval", "execute")
builder.add_edge("execute", END)


def main():
    # 체크포인터 필수! 없으면 interrupt 시점에 오류가 납니다.
    with SqliteSaver.from_conn_string("checkpoints.sqlite") as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "hitl_demo1"}}
        
        # 1차 invoke: interrupt에서 멈춥니다
        print("[1차 invoke]")
        graph.invoke({"messages": [HumanMessage("작업 시작")]}, config=config)

        # 멈춘 상태 확인
        state = graph.get_state(config)
        print("  멈춘 노드:", state.next)                       # ('approval',)
        print("  요청 내용:", state.tasks[0].interrupts[0].value)

        # 사람에게 묻습니다 (실제 서비스에서는 웹 UI나 메신저 버튼)
        user_decision = input("\n승인하시겠습니까? (yes/no): ").strip()

        # 2차 invoke: Command로 재개합니다
        print("\n[2차 invoke]")
        result = graph.invoke(Command(resume=user_decision), config=config)
        print("  최종:", get_text(result["messages"][-1]))
        


if __name__ == "__main__":
    main()