# conditional.py - 조건부 엣지로 분기하기
from typing import TypedDict, Literal
from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END
from langchain_aws import ChatBedrockConverse

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


class ClassifyState(TypedDict):
    user_input: str
    intent: str
    response: str


def classify(state: ClassifyState) -> dict:
    """입력에 '?'가 있으면 question, 없으면 exclaim으로 분류합니다."""
    intent = "question" if "?" in state["user_input"] else "exclaim"
    return {"intent": intent}


def answer_question(state: ClassifyState) -> dict:
    """질문에 LLM으로 답합니다."""
    response = llm.invoke(f"다음 질문에 한 줄로 답하세요: {state['user_input']}")
    return {"response": get_text(response)}


def react_exclaim(state: ClassifyState) -> dict:
    """감탄에는 LLM 없이 정해진 반응을 합니다."""
    return {"response": f"'{state['user_input']}' 라니 멋진 일이네요."}


def route_by_intent(state: ClassifyState) -> Literal["answer_question", "react_exclaim"]:
    """라우팅 함수: State를 보고 다음 노드의 이름을 반환합니다."""
    if state["intent"] == "question":
        return "answer_question"
    return "react_exclaim"

def is_question(state: ClassifyState) -> bool:
    return state["intent"] == "question"

builder = StateGraph(ClassifyState)
builder.add_node("classify", classify)
builder.add_node("answer_question", answer_question)
builder.add_node("react_exclaim", react_exclaim)

builder.add_edge(START, "classify")
builder.add_conditional_edges("classify", route_by_intent)   # 분기 등록

#builder.add_conditional_edges("classify",is_question, {True: "answer_question", False: "react_exclaim"})
builder.add_edge("answer_question", END)
builder.add_edge("react_exclaim", END)
graph = builder.compile()

for text in ["오늘 사내 식당 메뉴 뭐야?", "와 배포가 한 번에 성공했다", "LangGraph가 뭐야?"]:
    result = graph.invoke({"user_input": text, "intent": "", "response": ""})
    print(f"입력: {text}")
    print(f"분류: {result['intent']} / 응답: {result['response']}\n")