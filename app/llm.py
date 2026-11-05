"""역할별 LLM 호출 래퍼(Bedrock Converse). D-19.

- 역할(MAIN/SUB/READ)로 모델을 고른다. 모델 ID는 `app/config.py`로만 읽는다.
- 보내기 직전에 `boundary.assert_outbound_clean`으로 누출을 막는다(실패하면 호출하지 않는다).
- MAIN이 쓰로틀링이면 백오프 재시도(최대 `MAX_RETRIES`) 뒤 SUB로만 폴백한다. 다른 오류, READ·SUB의 쓰로틀링에는 폴백하지 않고
  `LLMUnavailable`로 멈춘다. 폴백해도 마스킹·누출 검사·한도는 그대로다(같은 경로를 지나고 토큰은 같은 예산에 합산).
- 요청한 역할과 실제 역할을 결과와 감사 기록에 남긴다. 오류 메시지에는 길이를 잘라 올리고 입력 텍스트는 담지 않는다.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

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
    content: list[dict]                 # Converse 응답 블록(text, toolUse)
    stop_reason: str
    requested_role: str
    actual_role: str
    model_id: str
    input_tokens: int
    output_tokens: int

    @property
    def fell_back(self) -> bool:
        return self.requested_role != self.actual_role

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content)

    @property
    def tool_uses(self) -> list[dict]:
        return [b["toolUse"] for b in self.content if "toolUse" in b]


def flatten_text(system: str | None, messages: list[dict], tools: list[dict] | None = None) -> str:
    """누출 검사용: 모델로 나가는 모든 텍스트(시스템, 메시지의 text·toolUse 입력·toolResult, 도구 설명)."""
    parts: list[str] = [system or ""]
    for m in messages:
        for b in m.get("content", []):
            if "text" in b:
                parts.append(b["text"])
            elif "toolUse" in b:
                parts.append(json.dumps(b["toolUse"].get("input", {}), ensure_ascii=False))
            elif "toolResult" in b:
                for c in b["toolResult"].get("content", []):
                    parts.append(c["text"] if "text" in c else json.dumps(c.get("json", {}), ensure_ascii=False))
    if tools:
        parts.append(json.dumps(tools, ensure_ascii=False))
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
        usage=None,
    ):
        if client is None:
            import boto3
            from botocore.config import Config
            # 큰 계획은 한 번의 응답이 1~3분 걸린다(출력 8K 토큰 이상). 기본 읽기 제한(60초)에 걸려 끊기지 않게 늘리고, 재시도는 우리 쪽 백오프가 맡는다(쓰로틀링을 두드리지 않게 SDK 재시도는 줄인다).
            client = boto3.client("bedrock-runtime", region_name=config.region(), config=Config(read_timeout=300, connect_timeout=10, retries={"max_attempts": 2, "mode": "standard"}))
        self._client, self._masker, self._budget, self._audit, self._sleep = client, masker, budget, audit, sleep
        self._usage = usage                                              # `app.usage.UsageLog`(선택): 모델별 날짜별 토큰·쓰로틀링 기록(D-53)

    def complete(
        self, role: str, user: str, *, system: str | None = None, max_tokens: int = 1024,
        temperature: float = 0.0, session_id: str | None = None,
    ) -> LLMResult:
        r = self._dispatch(role, [{"role": "user", "content": [{"text": user}]}], system, None, max_tokens, temperature, session_id)
        text = "".join(c.get("text", "") for c in r.content)
        return LLMResult(text, r.requested_role, r.actual_role, r.model_id, r.input_tokens, r.output_tokens)

    def chat(
        self, role: str, messages: list[dict], *, system: str | None = None, tools: list[dict] | None = None,
        max_tokens: int = 1024, temperature: float = 0.0, session_id: str | None = None,
    ) -> ChatResult:
        """여러 턴·도구 호출(Converse toolConfig)이 있는 대화 한 번. 검사·폴백·예산은 `complete`와 같은 경로를 쓴다."""
        return self._dispatch(role, messages, system, tools, max_tokens, temperature, session_id)

    # ---- 내부 ----
    def _dispatch(self, role, messages, system, tools, max_tokens, temperature, session_id) -> ChatResult:
        role = role.upper()
        if role not in ROLES:
            raise ValueError(f"알 수 없는 역할: {role}")
        # 마스킹이 끝난 텍스트만 나간다. 누출이면 LeakError로 멈춘다(재시도하지 않는다).
        assert_outbound_clean(flatten_text(system, messages, tools), self._masker, f"llm:{role}")
        try:
            return self._call_with_retry(role, role, messages, system, tools, max_tokens, temperature)
        except _Throttled:
            if role != "MAIN":
                raise LLMUnavailable(f"{role} 모델이 쓰로틀링 중이라 중단한다") from None
        # MAIN → SUB 폴백(한 방향, 같은 입력, 같은 검사)
        self._log(session_id, True, ("llm_fallback:MAIN->SUB",))
        try:
            return self._call_with_retry(role, "SUB", messages, system, tools, max_tokens, temperature)
        except _Throttled:
            self._log(session_id, False, ("llm_unavailable:MAIN,SUB",))
            raise LLMUnavailable("MAIN과 SUB 모델이 모두 쓰로틀링 중이라 중단한다") from None

    def _call_with_retry(self, requested: str, actual: str, messages, system, tools, max_tokens, temperature) -> ChatResult:
        model_id = config.model_id(actual)
        body = {
            "modelId": model_id,
            "messages": messages,
            "inferenceConfig": {"temperature": temperature, "maxTokens": max_tokens},
        }
        if system:
            body["system"] = [{"text": system}]
        if tools:
            body["toolConfig"] = {"tools": tools}
        for attempt in range(MAX_RETRIES + 1):
            try:
                r = self._client.converse(**body)
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
            usage = r.get("usage", {})
            in_t, out_t = int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0))
            if self._budget is not None:
                self._budget.add_tokens(in_t + out_t)
            if self._usage is not None:
                self._usage.record_call(model_id, actual, in_t, out_t)
            return ChatResult(list(r["output"]["message"]["content"]), str(r.get("stopReason", "")), requested, actual, model_id, in_t, out_t)
        raise _Throttled  # 도달하지 않는다

    def _log(self, session_id, allowed: bool, reasons: tuple[str, ...]) -> None:
        if self._audit is not None:
            self._audit.record(session_id, "limits", allowed, reasons)


class _Throttled(Exception):
    pass
