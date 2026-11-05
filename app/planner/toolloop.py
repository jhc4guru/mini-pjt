"""도구 호출 대화 루프(계획 에이전트와 실패 분석 에이전트가 공유한다).

한 단계(`stage`)는 모델이 제출 통로 도구를 호출해 서버가 수락할 때까지 모델과 도구 호출을 주고받는 것이다.
- 읽기 도구는 `ReadToolbox`로 실행한다(상한 초과는 예외로 전파되어 중단). 제출 통로는 아무것도 실행하지 않고 핸들러가 검증만 한다.
- 목록에 없는 도구, 단계에 맞지 않는 제출, 평문 답변은 오류로 돌려주고 수정 기회를 소모한다(`max_repairs`, `max_nudges`).
- 모델 출력에 실제 값이 있으면 대화에 남기지 않고 중단한다(`LoopStop("model_output_unsafe")`). 잘린 출력은 대화에 넣지 않고 줄여서 다시 요청한다.
- `compact=True`면 수락된 뒤 그 단계의 중간 대화를 버리고 수락된 제출만 짧게 남긴다(호출마다 대화 전체가 다시 실리므로 입력 토큰이 늘어나는 것을 막는다).
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any, Callable

from pydantic import ValidationError

from app.agents.tools import ReadToolbox
from app.guards.agent_limits import READ_ONLY_TOOLS, AgentBudget
from app.guards.boundary import leak_kinds
from app.guards.masking import Masker
from app.llm import LLMClient, flatten_text


class LoopStop(Exception):
    """대화를 계속할 수 없어 사람에게 넘긴다. code는 감사·표시용."""

    def __init__(self, code: str, detail: tuple[str, ...] = ()):
        super().__init__(code)
        self.code = code
        self.detail = detail


def tool_result(use: dict, payload: dict, ok: bool) -> dict:
    return {"toolResult": {"toolUseId": use["toolUseId"], "status": "success" if ok else "error", "content": [{"json": payload}]}}


def schema_codes(e: ValidationError) -> list[str]:
    """입력 값은 싣지 않고 위치와 오류 종류만 돌려준다."""
    return [f"schema:{'.'.join(str(p) for p in err['loc'])}:{err['type']}" for err in e.errors()[:6]]


class ToolLoop:
    def __init__(
        self, *, llm: LLMClient, toolbox: ReadToolbox, masker: Masker, budget: AgentBudget, system: str, tools: list[dict],
        submit_tools: frozenset[str], role: str = "MAIN", max_tokens: int = 8192, max_repairs: int = 2, max_nudges: int = 2,
        session_id: str | None = None, truncated_hint: str = "직전 출력이 길이 한도에서 잘렸다. 더 짧게 줄여 다시 제출하라.",
    ):
        self._llm, self._toolbox, self._masker, self._budget = llm, toolbox, masker, budget
        self._system, self._tools, self._submit_tools, self._role = system, tools, submit_tools, role
        self._max_tokens, self._max_repairs, self._max_nudges, self._session = max_tokens, max_repairs, max_nudges, session_id
        self._hint = truncated_hint
        self.messages: list[dict] = []
        self.pending: list[dict] = []
        self.roles: list[str] = []
        self.token_log: list[tuple[int, int]] = []
        self.repairs = 0
        self.repair_log: list[str] = []
        self.docs_seen: dict[str, tuple[str, str]] = {}      # `search_docs`로 찾은 문서 명령 id → (명령, 종류). 단계를 압축해도 요약으로 다시 줄 수 있다

    def user(self, text: str | None) -> None:
        content = [*self.pending] + ([{"text": text}] if text else [])
        self.pending = []
        if content:
            self.messages.append({"role": "user", "content": content})

    def usage(self) -> dict:
        return self._budget.usage()

    def stage(self, handlers: dict[str, Callable[[dict], tuple[bool, Any]]], *, compact: bool = False) -> tuple[str, Any]:
        """제출 통로가 수락될 때까지 진행한다. 성공하면 (도구 이름, 핸들러가 돌려준 값)."""
        stage_start = len(self.messages)
        repairs = nudges = 0
        while True:
            self._budget.before_step()
            res = self._llm.chat(self._role, self.messages, system=self._system, tools=self._tools,
                                 max_tokens=self._max_tokens, session_id=self._session)
            self.roles.append(f"{res.requested_role}->{res.actual_role}")
            self.token_log.append((res.input_tokens, res.output_tokens))
            blocks = [b for b in res.content if isinstance(b, dict)] if isinstance(res.content, list) else []
            text_kinds = leak_kinds(flatten_text(None, [{"content": [b for b in res.content if not (isinstance(b, dict) and "toolUse" in b)]}]), self._masker)
            if text_kinds:
                raise LoopStop("model_output_unsafe", detail=tuple(text_kinds))   # 글에 누출이 있으면 대화에 남기지 않고 멈춘다(종류 이름만 남긴다)
            unsafe: dict[str, list[str]] = {}                  # 도구 호출 인자의 누출: 실행하지 않고 오류로 돌려 고치게 한다(D-45)
            for b in blocks:
                tu = b.get("toolUse")
                if tu:
                    k = leak_kinds(json.dumps(tu.get("input"), ensure_ascii=False, default=str), self._masker)
                    if k:
                        unsafe[tu["toolUseId"]] = k
            if unsafe:
                res = dataclasses.replace(res, content=[({"toolUse": {**b["toolUse"], "input": {}}} if isinstance(b, dict) and "toolUse" in b and b["toolUse"]["toolUseId"] in unsafe else b)
                                                        for b in res.content])             # 누출된 입력은 대화에 남기지 않는다
            if res.stop_reason == "max_tokens":                    # 잘린 출력은 대화에 넣지 않고 줄여서 다시 요청한다
                repairs += 1
                self.repairs += 1
                if repairs > self._max_repairs:
                    raise LoopStop("output_truncated")
                self.messages[-1]["content"].append({"text": self._hint})
                continue
            self.messages.append({"role": "assistant", "content": res.content})
            uses = res.tool_uses
            if not uses:
                nudges += 1
                if nudges > self._max_nudges:
                    raise LoopStop("no_submission")
                self.user("평문 답변은 쓰지 않는다. 필요한 도구를 호출하거나 제출 도구로 결과를 제출하라.")
                continue
            results: list[dict] = []
            accepted: tuple[str, Any] | None = None
            accepted_use: dict | None = None
            for use in uses:
                name, inp = use["name"], use.get("input") or {}
                if use["toolUseId"] in unsafe:
                    repairs += 1
                    self.repairs += 1
                    self.repair_log.append(f"{name}:tool_input_unsafe:{','.join(unsafe[use['toolUseId']])}")
                    if repairs > self._max_repairs:
                        raise LoopStop("model_output_unsafe", detail=tuple(unsafe[use["toolUseId"]]))
                    results.append(tool_result(use, {"error": ["tool_input_contains_sensitive_value", *unsafe[use["toolUseId"]]],
                                                     "hint": "도구 입력에 계정 ID·리소스 ID·IP 같은 민감값 형태가 있어 실행하지 않았다. 값을 쓰지 말고 플레이스홀더나 SSM 공개 파라미터를 써라."}, False))
                    continue
                if name in READ_ONLY_TOOLS:
                    out = self._toolbox.call(name, inp)             # 상한 초과는 예외로 전파된다
                    if name == "search_docs" and out.get("ok"):
                        for h in out.get("hits", []):
                            self.docs_seen[h["id"]] = (h["command"], h["kind"])
                    results.append(tool_result(use, out, bool(out.get("ok"))))
                elif name in handlers and accepted is None:
                    ok, value = handlers[name](inp)
                    if ok:
                        accepted, accepted_use = (name, value), use
                        results.append(tool_result(use, {"accepted": True}, True))
                    else:
                        repairs += 1
                        self.repairs += 1
                        self.repair_log += [f"{name}:{c}" for c in value[:6]]
                        results.append(tool_result(use, {"error": value}, False))
                elif name in self._submit_tools:
                    repairs += 1
                    self.repairs += 1
                    results.append(tool_result(use, {"error": ["wrong_stage" if name not in handlers else "duplicate_submission"]}, False))
                else:
                    repairs += 1
                    self.repairs += 1
                    results.append(tool_result(use, {"error": ["unknown_tool"]}, False))
            self.pending = results
            if accepted is not None:
                if compact and accepted_use is not None:
                    kept = {"toolUse": {**accepted_use, "input": {"accepted_steps": accepted[1]}}}
                    self.messages[stage_start:] = [{"role": "assistant", "content": [kept]}]
                    self.pending = [tool_result(accepted_use, {"accepted": True}, True)]
                return accepted                                     # 결과는 다음 사용자 메시지 앞에 붙는다
            if repairs > self._max_repairs:
                raise LoopStop("repair_exhausted")
            self.user(None)


def inline_refs(schema: dict) -> dict:
    """Converse 도구 스키마용으로 `$ref`를 인라인으로 풀고 `$defs`를 없앤다."""
    import copy

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


def submit_spec(name: str, description: str, model) -> dict:
    """Pydantic 모델을 Converse 제출 통로 도구 정의로 바꾼다."""
    return {"toolSpec": {"name": name, "description": description, "inputSchema": {"json": inline_refs(model.model_json_schema())}}}
