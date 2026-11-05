"""도구 호출 대화 루프(계획 에이전트와 실패 분석 에이전트가 공유한다). LangGraph `StateGraph`로 구현한다(D-53).

한 단계(`stage`)는 모델이 제출 통로 도구를 호출해 서버가 수락할 때까지 모델과 도구 호출을 주고받는 것이다. 그래프는 다음과 같다.

    START → model ─(도구 호출 있음)→ tools ─(수락)→ END
              ↑  ╲(없음·잘림: 수정 요청)╱      │(거절·읽기 도구 결과)
              └──────────────────────────────┘

- `model` 노드는 `LLMClient.chat`(발송 전 누출 검사, MAIN→SUB 폴백, 토큰 합산)만 거쳐 모델을 부른다. 글에 누출이 있으면 중단한다.
- `tools` 노드는 읽기 도구를 LangChain 도구(`ReadToolbox.as_tools`)로 실행한다(상한 초과는 예외로 전파되어 중단). 제출 통로는 아무것도
  실행하지 않고 핸들러가 검증만 한다.
- 목록에 없는 도구, 단계에 맞지 않는 제출, 평문 답변은 오류로 돌려주고 수정 기회를 소모한다(`max_repairs`, `max_nudges`).
- 모델 출력에 실제 값이 있으면 대화에 남기지 않고 중단한다(`LoopStop("model_output_unsafe")`). 잘린 출력은 대화에 넣지 않고 줄여서 다시 요청한다.
- `compact=True`면 수락된 뒤 그 단계의 중간 대화를 버리고 수락된 제출만 짧게 남긴다(호출마다 대화 전체가 다시 실리므로 입력 토큰이 늘어나는 것을 막는다).
- 반복 횟수의 상한은 그래프의 `recursion_limit`이 아니라 `AgentBudget`(단계·호출·토큰·시간)과 수정 기회가 정한다. 그래프 한도는 안전망이다.
"""
from __future__ import annotations

import copy
import json
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from app.guards.agent_limits import READ_ONLY_TOOLS, AgentBudget
from app.guards.boundary import leak_kinds
from app.guards.masking import Masker
from app.llm import LLMClient, flatten_text, message_text

GRAPH_RECURSION_LIMIT = 2000      # 안전망. 실제 상한은 AgentBudget과 수정 기회다.


class LoopStop(Exception):
    """대화를 계속할 수 없어 사람에게 넘긴다. code는 감사·표시용."""

    def __init__(self, code: str, detail: tuple[str, ...] = ()):
        super().__init__(code)
        self.code = code
        self.detail = detail


def tool_result(call: dict, payload: dict, ok: bool) -> ToolMessage:
    return ToolMessage(content=json.dumps(payload, ensure_ascii=False), tool_call_id=call["id"], status="success" if ok else "error")


def schema_codes(e: ValidationError) -> list[str]:
    """입력 값은 싣지 않고 위치와 오류 종류만 돌려준다."""
    return [f"schema:{'.'.join(str(p) for p in err['loc'])}:{err['type']}" for err in e.errors()[:6]]


class StageState(TypedDict):
    """한 단계의 그래프 상태. `messages`는 `add_messages`로 누적한다."""
    messages: Annotated[list[BaseMessage], add_messages]
    repairs: int                         # 이 단계에서 쓴 수정 기회
    nudges: int                          # 도구 없이 평문으로 답해 다시 요청한 횟수
    accepted: tuple[str, Any] | None     # 수락된 제출 (도구 이름, 핸들러 값)
    accepted_call: dict | None
    pending: list[ToolMessage]           # 수락된 호출의 결과(다음 사용자 메시지 앞에 붙는다)


class ToolLoop:
    def __init__(
        self, *, llm: LLMClient, toolbox, masker: Masker, budget: AgentBudget, system: str, tools: list[BaseTool],
        submit_tools: frozenset[str], role: str = "MAIN", max_tokens: int = 8192, max_repairs: int = 2, max_nudges: int = 2,
        session_id: str | None = None, truncated_hint: str = "직전 출력이 길이 한도에서 잘렸다. 더 짧게 줄여 다시 제출하라.",
    ):
        self._llm, self._toolbox, self._masker, self._budget = llm, toolbox, masker, budget
        self._system, self._tools, self._submit_tools, self._role = system, tools, submit_tools, role
        self._max_tokens, self._max_repairs, self._max_nudges, self._session = max_tokens, max_repairs, max_nudges, session_id
        self._hint = truncated_hint
        self._tool_by_name = {t.name: t for t in tools}
        self.messages: list[BaseMessage] = []
        self.pending: list[ToolMessage] = []
        self.roles: list[str] = []
        self.token_log: list[tuple[int, int]] = []       # 호출별 (입력, 출력). 입력은 캐시 읽기·쓰기를 포함한 전체
        self.cache_log: list[tuple[int, int]] = []       # 호출별 (캐시 읽기, 캐시 쓰기) 토큰
        self.repairs = 0
        self.repair_log: list[str] = []
        self.docs_seen: dict[str, tuple[str, str]] = {}      # `search_docs`로 찾은 문서 명령 id → (명령, 종류). 단계를 압축해도 요약으로 다시 줄 수 있다
        self.reads_seen: dict[str, str] = {}                 # `run_read_command`로 읽은 (마스킹된 명령 → 정리된 출력). 단계를 압축하면 대화에서 사라지므로 요약으로 다시 줄 수 있게 둔다(같은 조회의 반복 방지)
        self._unsafe: dict[str, list[str]] = {}              # 가장 최근 모델 응답의 도구 호출 인자 누출(호출 id → 종류)
        self._graph = self._build_graph()

    # ---- 그래프 ----
    def _build_graph(self):
        g = StateGraph(StageState)
        g.add_node("model", self._model_node)
        g.add_node("tools", self._tools_node)
        g.add_edge(START, "model")
        g.add_conditional_edges("model", self._after_model, {"model": "model", "tools": "tools"})
        g.add_conditional_edges("tools", self._after_tools, {"model": "model", "end": END})
        return g.compile()

    @staticmethod
    def _after_model(state: StageState) -> str:
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) else "model"          # AI 응답이 없으면(잘림·평문) 수정 요청이 붙어 다시 모델로

    @staticmethod
    def _after_tools(state: StageState) -> str:
        return "end" if state["accepted"] is not None else "model"

    def _model_node(self, state: StageState) -> dict:
        self._budget.before_step()
        res = self._llm.chat(self._role, state["messages"], system=self._system, tools=self._tools,
                             max_tokens=self._max_tokens, session_id=self._session, cache=True)
        self.roles.append(f"{res.requested_role}->{res.actual_role}")
        self.token_log.append((res.input_tokens, res.output_tokens))
        self.cache_log.append((res.cache_read_tokens, res.cache_write_tokens))
        text = res.text
        text_kinds = leak_kinds(flatten_text(None, [HumanMessage(content=text)]), self._masker)
        if text_kinds:
            raise LoopStop("model_output_unsafe", detail=tuple(text_kinds))   # 글에 누출이 있으면 대화에 남기지 않고 멈춘다(종류 이름만 남긴다)
        calls = [dict(c) for c in res.tool_calls]
        unsafe: dict[str, list[str]] = {}                      # 도구 호출 인자의 누출: 실행하지 않고 오류로 돌려 고치게 한다(D-45)
        for c in calls:
            k = leak_kinds(json.dumps(c.get("args"), ensure_ascii=False, default=str), self._masker)
            if k:
                unsafe[c["id"]] = k
        self._unsafe = unsafe
        if res.stop_reason == "max_tokens":                    # 잘린 출력은 대화에 넣지 않고 줄여서 다시 요청한다
            repairs = state["repairs"] + 1
            self.repairs += 1
            if repairs > self._max_repairs:
                raise LoopStop("output_truncated")
            return {"repairs": repairs, "messages": [HumanMessage(content=self._hint)]}
        stored = AIMessage(content=text, tool_calls=[({**c, "args": {}} if c["id"] in unsafe else c) for c in calls])   # 누출된 입력은 대화에 남기지 않는다
        if not calls:
            nudges = state["nudges"] + 1
            if nudges > self._max_nudges:
                raise LoopStop("no_submission")
            return {"nudges": nudges, "messages": [stored, HumanMessage(content="평문 답변은 쓰지 않는다. 필요한 도구를 호출하거나 제출 도구로 결과를 제출하라.")]}
        return {"messages": [stored]}

    def _tools_node(self, state: StageState, config: RunnableConfig) -> dict:
        handlers: dict[str, Callable[[dict], tuple[bool, Any]]] = config["configurable"]["handlers"]
        last = state["messages"][-1]
        repairs = state["repairs"]
        results: list[ToolMessage] = []
        accepted: tuple[str, Any] | None = None
        accepted_call: dict | None = None
        for call in last.tool_calls:
            name, inp = call["name"], call.get("args") or {}
            if call["id"] in self._unsafe:
                repairs += 1
                self.repairs += 1
                self.repair_log.append(f"{name}:tool_input_unsafe:{','.join(self._unsafe[call['id']])}")
                if repairs > self._max_repairs:
                    raise LoopStop("model_output_unsafe", detail=tuple(self._unsafe[call["id"]]))
                results.append(tool_result(call, {"error": ["tool_input_contains_sensitive_value", *self._unsafe[call["id"]]],
                                                  "hint": "도구 입력에 계정 ID·리소스 ID·IP 같은 민감값 형태가 있어 실행하지 않았다. 값을 쓰지 말고 플레이스홀더나 SSM 공개 파라미터를 써라."}, False))
                continue
            if name in READ_ONLY_TOOLS:
                out = self._tool_by_name[name].invoke(inp)         # 상한 초과는 예외로 전파된다
                if name == "search_docs" and out.get("ok"):
                    for h in out.get("hits", []):
                        self.docs_seen[h["id"]] = (h["command"], h["kind"])
                if name == "run_read_command" and out.get("ok"):
                    self.reads_seen.pop(out["command"], None)      # 같은 명령을 다시 읽으면 최신 결과로 바꾸고 순서를 뒤로 보낸다
                    self.reads_seen[out["command"]] = out["output"]
                results.append(tool_result(call, out, bool(out.get("ok"))))
            elif name in handlers and accepted is None:
                ok, value = handlers[name](inp)
                if ok:
                    accepted, accepted_call = (name, value), call
                    results.append(tool_result(call, {"accepted": True}, True))
                else:
                    repairs += 1
                    self.repairs += 1
                    self.repair_log += [f"{name}:{c}" for c in value[:6]]
                    results.append(tool_result(call, {"error": value}, False))
            elif name in self._submit_tools:
                repairs += 1
                self.repairs += 1
                results.append(tool_result(call, {"error": ["wrong_stage" if name not in handlers else "duplicate_submission"]}, False))
            else:
                repairs += 1
                self.repairs += 1
                results.append(tool_result(call, {"error": ["unknown_tool"]}, False))
        if accepted is not None:
            return {"repairs": repairs, "accepted": accepted, "accepted_call": accepted_call, "pending": results}   # 결과는 다음 사용자 메시지 앞에 붙는다
        if repairs > self._max_repairs:
            raise LoopStop("repair_exhausted")
        return {"repairs": repairs, "messages": results}

    # ---- 공개 ----
    def user(self, text: str | None) -> None:
        msgs: list[BaseMessage] = [*self.pending] + ([HumanMessage(content=text)] if text else [])
        self.pending = []
        self.messages.extend(msgs)

    def usage(self) -> dict:
        return self._budget.usage()

    def stage(self, handlers: dict[str, Callable[[dict], tuple[bool, Any]]], *, compact: bool = False) -> tuple[str, Any]:
        """제출 통로가 수락될 때까지 진행한다. 성공하면 (도구 이름, 핸들러가 돌려준 값)."""
        stage_start = len(self.messages)
        state = self._graph.invoke(
            {"messages": list(self.messages), "repairs": 0, "nudges": 0, "accepted": None, "accepted_call": None, "pending": []},
            {"configurable": {"handlers": handlers}, "recursion_limit": GRAPH_RECURSION_LIMIT},
        )
        self.messages = list(state["messages"])
        self.pending = list(state["pending"])
        accepted, accepted_call = state["accepted"], state["accepted_call"]
        if compact and accepted_call is not None:
            kept = AIMessage(content="", tool_calls=[{"name": accepted_call["name"], "args": {"accepted_steps": accepted[1]}, "id": accepted_call["id"], "type": "tool_call"}])
            self.messages[stage_start:] = [kept]
            self.pending = [tool_result(accepted_call, {"accepted": True}, True)]
        return accepted


def inline_refs(schema: dict) -> dict:
    """모델에 보내는 도구 스키마용으로 `$ref`를 인라인으로 풀고 `$defs`를 없앤다."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items() if k not in ("$defs", "title")}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


def _never_run(**_: Any) -> None:
    raise RuntimeError("제출 통로 도구는 실행하지 않는다(핸들러가 검증만 한다)")


def submit_spec(name: str, description: str, model) -> StructuredTool:
    """Pydantic 모델을 제출 통로 LangChain 도구로 바꾼다. 서버는 이 도구를 실행하지 않고 핸들러로 검증만 한다."""
    return StructuredTool(name=name, description=description, args_schema=inline_refs(model.model_json_schema()), func=_never_run)
