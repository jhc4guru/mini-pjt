"""역할별 LLM 호출 래퍼(Bedrock Converse). D-19.

- 역할(MAIN/SUB/READ)로 모델을 고른다. 모델 ID는 `app/config.py`로만 읽는다.
- 보내기 직전에 `boundary.assert_outbound_clean`으로 누출을 막는다(실패하면 호출하지 않는다).
- MAIN이 쓰로틀링이면 백오프 재시도(최대 `MAX_RETRIES`) 뒤 SUB로만 폴백한다. 다른 오류, READ·SUB의 쓰로틀링에는 폴백하지 않고
  `LLMUnavailable`로 멈춘다. 폴백해도 마스킹·누출 검사·한도는 그대로다(같은 경로를 지나고 토큰은 같은 예산에 합산).
- 요청한 역할과 실제 역할을 결과와 감사 기록에 남긴다. 오류 메시지에는 길이를 잘라 올리고 입력 텍스트는 담지 않는다.
"""
from __future__ import annotations

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
    ):
        if client is None:
            import boto3
            client = boto3.client("bedrock-runtime", region_name=config.region())
        self._client, self._masker, self._budget, self._audit, self._sleep = client, masker, budget, audit, sleep

    def complete(
        self, role: str, user: str, *, system: str | None = None, max_tokens: int = 1024,
        temperature: float = 0.0, session_id: str | None = None,
    ) -> LLMResult:
        role = role.upper()
        if role not in ROLES:
            raise ValueError(f"알 수 없는 역할: {role}")
        # 마스킹이 끝난 텍스트만 나간다. 누출이면 LeakError로 멈춘다(재시도하지 않는다).
        assert_outbound_clean(f"{system or ''}\n{user}", self._masker, f"llm:{role}")
        try:
            return self._call_with_retry(role, role, user, system, max_tokens, temperature)
        except _Throttled:
            if role != "MAIN":
                raise LLMUnavailable(f"{role} 모델이 쓰로틀링 중이라 중단한다") from None
        # MAIN → SUB 폴백(한 방향, 같은 입력, 같은 검사)
        self._log(session_id, True, ("llm_fallback:MAIN->SUB",))
        try:
            return self._call_with_retry(role, "SUB", user, system, max_tokens, temperature)
        except _Throttled:
            self._log(session_id, False, ("llm_unavailable:MAIN,SUB",))
            raise LLMUnavailable("MAIN과 SUB 모델이 모두 쓰로틀링 중이라 중단한다") from None

    def _call_with_retry(self, requested: str, actual: str, user, system, max_tokens, temperature) -> LLMResult:
        model_id = config.model_id(actual)
        body = {
            "modelId": model_id,
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": {"temperature": temperature, "maxTokens": max_tokens},
        }
        if system:
            body["system"] = [{"text": system}]
        for attempt in range(MAX_RETRIES + 1):
            try:
                r = self._client.converse(**body)
            except Exception as e:  # noqa: BLE001
                if _error_code(e) in THROTTLING_CODES:
                    if attempt < MAX_RETRIES:
                        self._sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
                        continue
                    raise _Throttled from None
                # 그 밖의 오류는 폴백 없이 멈춘다. 메시지는 길이를 잘라 올린다(계정 정보가 섞일 수 있다).
                raise LLMUnavailable(f"{actual} 호출 실패: {type(e).__name__} {_error_code(e)}"[:120]) from None
            usage = r.get("usage", {})
            in_t, out_t = int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0))
            if self._budget is not None:
                self._budget.add_tokens(in_t + out_t)
            text = "".join(c.get("text", "") for c in r["output"]["message"]["content"])
            return LLMResult(text, requested, actual, model_id, in_t, out_t)
        raise _Throttled  # 도달하지 않는다

    def _log(self, session_id, allowed: bool, reasons: tuple[str, ...]) -> None:
        if self._audit is not None:
            self._audit.record(session_id, "limits", allowed, reasons)


class _Throttled(Exception):
    pass
