"""역할별 LLM 호출 래퍼(LangChain `ChatBedrockConverse`). D-19, D-53.

- 역할(MAIN/SUB/READ)로 모델을 고른다. 모델 ID는 `app/config.py`로만 읽는다.
- 모델 호출은 LangChain `ChatBedrockConverse`가 하고, 메시지는 LangChain 메시지(`HumanMessage`·`AIMessage`·`ToolMessage`)다.
  모델 호출은 이 모듈 한 곳을 거친다(그래프 노드도 `LLMClient.chat`만 부른다).
- 보내기 직전에 `boundary.assert_outbound_clean`으로 누출을 막는다(실패하면 호출하지 않는다).
- MAIN이 쓰로틀링이면 백오프 재시도(최대 `MAX_RETRIES`) 뒤 SUB로만 폴백한다. 다른 오류, READ·SUB의 쓰로틀링에는 폴백하지 않고
  `LLMUnavailable`로 멈춘다. 폴백해도 마스킹·누출 검사·한도는 그대로다(같은 경로를 지나고 토큰은 같은 예산에 합산).
- 요청한 역할과 실제 역할을 결과와 감사 기록에 남긴다. 오류 메시지에는 길이를 잘라 올리고 입력 텍스트는 담지 않는다.
- 프롬프트 캐싱(D-54): `prompt_cache=True`인 클라이언트는 도구 루프(`chat(cache=True)`)에서 시스템 프롬프트·도구 정의·대화 끝에 캐시 지점을
  넣는다(`langchain-aws`의 `cache_control`). 모델·리전이 거부하면(`ValidationException`) 요청을 실패시키지 않고 캐시 없이 다시 보낸다.
  누출 검사는 캐시와 무관하게 같은 텍스트에 대해 먼저 한다. 호출별 캐시 읽기·쓰기 토큰을 결과에 싣는다.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from app import config
from app.guards.agent_limits import AgentBudget
from app.guards.audit import AuditLog
from app.guards.boundary import assert_outbound_clean
from app.guards.masking import Masker

THROTTLING_CODES = frozenset({"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"})
MAX_RETRIES = 2
BACKOFF_SECONDS = (1.0, 3.0)
ROLES = ("MAIN", "SUB", "READ")


class LLMUnavailable(RuntimeError):
    """모델을 쓸 수 없어 중단한다(사람에게 알린다)."""


def message_text(message: BaseMessage) -> str:
    """메시지 내용에서 글(text)만 모은다. `ChatBedrockConverse`는 content를 블록 리스트로 주기도 한다."""
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type", "text") == "text")


@dataclass(frozen=True)
class LLMResult:
    text: str
    requested_role: str
    actual_role: str
    model_id: str
    input_tokens: int
    output_tokens: int

    @property
    def fell_back(self) -> bool:
        return self.requested_role != self.actual_role


@dataclass(frozen=True)
class ChatResult:
    message: AIMessage                  # 모델 응답(글과 도구 호출)
    stop_reason: str
    requested_role: str
    actual_role: str
    model_id: str
    input_tokens: int                   # 캐시 읽기·쓰기를 포함한 전체 입력 토큰(한도 계산은 이 값으로 한다)
    output_tokens: int
    cache_read_tokens: int = 0          # 그중 캐시에서 읽은 토큰
    cache_write_tokens: int = 0         # 그중 캐시에 새로 쓴 토큰

    @property
    def fell_back(self) -> bool:
        return self.requested_role != self.actual_role

    @property
    def text(self) -> str:
        return message_text(self.message)

    @property
    def tool_calls(self) -> list[dict]:
        return list(self.message.tool_calls)


def tools_text(tools: Sequence[Any] | None) -> str:
    """누출 검사용: 모델에 보이는 도구 정의(이름·설명·스키마)."""
    if not tools:
        return ""
    return json.dumps([_tool_dict(t) for t in tools], ensure_ascii=False, default=str)


def _tool_dict(tool: Any) -> Any:
    if isinstance(tool, dict) and "toolSpec" in tool:
        return tool
    return convert_to_openai_tool(tool)


def flatten_text(system: str | None, messages: Sequence[BaseMessage], tools: Sequence[Any] | None = None) -> str:
    """누출 검사용: 모델로 나가는 모든 텍스트(시스템, 메시지 글, 도구 호출 인자, 도구 결과, 도구 설명)."""
    parts: list[str] = [system or ""]
    for m in messages:
        c = m.content
        if isinstance(c, str):
            parts.append(c)
        else:
            for b in c:
                if isinstance(b, dict):
                    if "text" in b:
                        parts.append(str(b["text"]))
                    elif b.get("type") == "tool_use":
                        parts.append(json.dumps(b.get("input", {}), ensure_ascii=False, default=str))
                    else:
                        parts.append(json.dumps(b, ensure_ascii=False, default=str))
                else:
                    parts.append(str(b))
        for call in getattr(m, "tool_calls", None) or []:
            parts.append(json.dumps(call.get("args", {}), ensure_ascii=False, default=str))
    parts.append(tools_text(tools))
    return "\n".join(parts)


def _error_code(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return str(resp.get("Error", {}).get("Code", ""))
    return ""


class LLMClient:
    def __init__(
        self,
        masker: Masker,
        *,
        client=None,
        budget: AgentBudget | None = None,
        audit: AuditLog | None = None,
        sleep: Callable[[float], None] = time.sleep,
        prompt_cache: bool = False,
        usage=None,
    ):
        if client is None:
            import boto3
            from botocore.config import Config
            # 큰 계획은 한 번의 응답이 1~3분 걸린다(출력 8K 토큰 이상). 기본 읽기 제한(60초)에 걸려 끊기지 않게 늘리고, 재시도는 우리 쪽 백오프가 맡는다(쓰로틀링을 두드리지 않게 SDK 재시도는 줄인다).
            client = boto3.client("bedrock-runtime", region_name=config.region(), config=Config(read_timeout=300, connect_timeout=10, retries={"max_attempts": 2, "mode": "standard"}))
        self._client, self._masker, self._budget, self._audit, self._sleep = client, masker, budget, audit, sleep
        self._prompt_cache = prompt_cache          # 켜져 있어도 모델·리전이 거부하면 꺼진다(`_cache_rejected`)
        self._cache_rejected = False
        self._usage = usage                                              # `app.usage.UsageLog`(선택): 모델별 날짜별 토큰·쓰로틀링 기록(D-53)

    def complete(
        self, role: str, user: str, *, system: str | None = None, max_tokens: int = 1024,
        temperature: float = 0.0, session_id: str | None = None,
    ) -> LLMResult:
        r = self._dispatch(role, [HumanMessage(content=user)], system, None, max_tokens, temperature, session_id)
        return LLMResult(r.text, r.requested_role, r.actual_role, r.model_id, r.input_tokens, r.output_tokens)

    def chat(
        self, role: str, messages: Sequence[BaseMessage], *, system: str | None = None, tools: Sequence[Any] | None = None,
        max_tokens: int = 1024, temperature: float = 0.0, session_id: str | None = None, cache: bool = False,
    ) -> ChatResult:
        """여러 턴·도구 호출이 있는 대화 한 번. 검사·폴백·예산은 `complete`와 같은 경로를 쓴다. `cache=True`면 프롬프트 캐싱을 쓴다(클라이언트가 켜져 있을 때)."""
        return self._dispatch(role, list(messages), system, tools, max_tokens, temperature, session_id, cache)

    # ---- 내부 ----
    def _dispatch(self, role, messages, system, tools, max_tokens, temperature, session_id, cache: bool = False) -> ChatResult:
        role = role.upper()
        if role not in ROLES:
            raise ValueError(f"알 수 없는 역할: {role}")
        # 마스킹이 끝난 텍스트만 나간다. 누출이면 LeakError로 멈춘다(재시도하지 않는다).
        assert_outbound_clean(flatten_text(system, messages, tools), self._masker, f"llm:{role}")
        try:
            return self._call_with_retry(role, role, messages, system, tools, max_tokens, temperature, cache)
        except _Throttled:
            if role != "MAIN":
                raise LLMUnavailable(f"{role} 모델이 쓰로틀링 중이라 중단한다") from None
        # MAIN → SUB 폴백(한 방향, 같은 입력, 같은 검사)
        self._log(session_id, True, ("llm_fallback:MAIN->SUB",))
        try:
            return self._call_with_retry(role, "SUB", messages, system, tools, max_tokens, temperature, cache)
        except _Throttled:
            self._log(session_id, False, ("llm_unavailable:MAIN,SUB",))
            raise LLMUnavailable("MAIN과 SUB 모델이 모두 쓰로틀링 중이라 중단한다") from None

    def _model(self, model_id: str, tools, max_tokens: int, temperature: float):
        chat = ChatBedrockConverse(
            client=self._client, model=model_id, region_name=config.region(), max_tokens=max_tokens,
            temperature=temperature, disable_streaming=True,
        )
        return chat.bind_tools(list(tools)) if tools else chat

    def _invoke(self, runnable, sent, cache: bool, actual: str):
        """모델 호출 한 번. 캐시를 쓰는 호출이 캐시 지점 거부(`ValidationException`)로 실패하면 캐시 없이 같은 요청을 한 번 다시 보낸다."""
        if not (cache and self._prompt_cache and not self._cache_rejected):
            return runnable.invoke(sent)
        try:
            return runnable.invoke(sent, cache_control={"ttl": "5m"})
        except Exception as e:  # noqa: BLE001
            if _error_code(e) != "ValidationException" or "cache" not in str(e).lower():
                raise
            self._cache_rejected = True                  # 모델·리전이 캐시 지점을 거부했다(메시지는 판별에만 쓰고 저장하지 않는다)
            self._log(None, True, (f"prompt_cache_rejected:{actual}",))
            return runnable.invoke(sent)

    def _call_with_retry(self, requested: str, actual: str, messages, system, tools, max_tokens, temperature, cache: bool = False) -> ChatResult:
        model_id = config.model_id(actual)
        runnable = self._model(model_id, tools, max_tokens, temperature)
        sent = ([SystemMessage(content=system)] if system else []) + list(messages)
        for attempt in range(MAX_RETRIES + 1):
            try:
                ai = self._invoke(runnable, sent, cache, actual)
            except Exception as e:  # noqa: BLE001
                if _error_code(e) in THROTTLING_CODES:
                    if self._usage is not None and attempt == MAX_RETRIES:
                        self._usage.record_throttle(model_id, actual)       # 재시도를 모두 쓴 쓰로틀링만 센다
                    if attempt < MAX_RETRIES:
                        self._sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
                        continue
                    raise _Throttled from None
                # 그 밖의 오류는 폴백 없이 멈춘다. 메시지는 길이를 잘라 올린다(계정 정보가 섞일 수 있다).
                self._log(None, False, (f"llm_error:{actual}:{type(e).__name__}:{_error_code(e) or '-'}"[:120],))      # 원인 종류만 남긴다(메시지는 계정 정보가 섞일 수 있어 남기지 않는다)
                raise LLMUnavailable(f"{actual} 호출 실패: {type(e).__name__} {_error_code(e)}"[:120]) from None
            usage = ai.usage_metadata or {}
            in_t, out_t = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
            details = usage.get("input_token_details") or {}
            c_read = int(details.get("cache_read", 0) or 0)
            # 쓰기 토큰: 응답에 TTL별 상세(`cacheDetails`)가 있으면 langchain-aws가 `cache_creation`을 0으로 두고 `ephemeral_*_input_tokens`로 옮긴다. 둘을 합친다(겹치지 않는다).
            c_write = sum(int(details.get(k, 0) or 0) for k in ("cache_creation", "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"))
            if self._budget is not None:
                self._budget.add_tokens(in_t + out_t)
            if self._usage is not None:
                self._usage.record_call(model_id, actual, in_t, out_t)
            stop = str((ai.response_metadata or {}).get("stopReason", ""))
            return ChatResult(ai, stop, requested, actual, model_id, in_t, out_t, c_read, c_write)
        raise _Throttled  # 도달하지 않는다

    def _log(self, session_id, allowed: bool, reasons: tuple[str, ...]) -> None:
        if self._audit is not None:
            self._audit.record(session_id, "limits", allowed, reasons)


class _Throttled(Exception):
    pass
