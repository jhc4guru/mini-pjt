"""단발 호출(접수·리뷰어·말로 수정)의 "호출 → 스키마 검증 → 재시도 → 사람에게 넘김" 흐름. LangGraph `StateGraph`(D-53).

    START → call → validate ─(통과)→ END
              ↑        │(실패, 시도 < 상한)
              └────────┘        └(실패, 상한 초과)→ SchemaViolation

- `call` 노드는 호출한 쪽이 넘긴 함수(`LLMClient.complete` 경유: 누출 검사·폴백·예산 그대로)를 실행한다.
- 검증 규칙은 가드의 `parse_structured` 하나다(`validate_structured`와 같은 규칙, 재시도 횟수도 같다).
- 예외(`LeakError`, `LLMUnavailable`, `GuardLimitError`)는 그래프를 통과해 호출한 쪽으로 그대로 전파된다.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Type, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from app.guards.agent_limits import parse_structured, schema_violation


class _State(TypedDict, total=False):
    raw: Any
    attempts: int
    value: Optional[BaseModel]
    last_error: Optional[Exception]


def _call_node(state: _State, config: RunnableConfig) -> dict:
    return {"raw": config["configurable"]["call"](), "attempts": state.get("attempts", 0) + 1}


def _validate_node(state: _State, config: RunnableConfig) -> dict:
    try:
        return {"value": parse_structured(state["raw"], config["configurable"]["model"]), "last_error": None}
    except (ValidationError, ValueError) as e:
        return {"value": None, "last_error": e}


def _fail_node(state: _State) -> dict:
    raise schema_violation(state["attempts"], state.get("last_error"))


def _route(state: _State, config: RunnableConfig) -> str:
    if state.get("value") is not None:
        return "end"
    return "call" if state["attempts"] <= config["configurable"]["max_retries"] else "fail"


def _build():
    g = StateGraph(_State)
    g.add_node("call", _call_node)
    g.add_node("validate", _validate_node)
    g.add_node("fail", _fail_node)
    g.add_edge(START, "call")
    g.add_edge("call", "validate")
    g.add_conditional_edges("validate", _route, {"call": "call", "end": END, "fail": "fail"})
    return g.compile()


STRUCTURED_GRAPH = _build()


def run_structured(call: Callable[[], Any], model: Type[BaseModel], *, max_retries: int = 2) -> BaseModel:
    """모델 출력을 스키마로 검증한다. 실패하면 재시도 상한까지 다시 호출하고, 그래도 안 되면 SchemaViolation(사람에게 넘김)."""
    state = STRUCTURED_GRAPH.invoke({"attempts": 0}, {"configurable": {"call": call, "model": model, "max_retries": max_retries}, "recursion_limit": 3 * (max_retries + 2) + 5})
    return state["value"]
