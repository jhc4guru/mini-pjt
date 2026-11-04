# guards.py - 오늘 만든 부품을 한곳에 모으는 파일
# 앞 실습에서 만든 파일들의 이름을 그대로 다시 내보냅니다.
# 새로 등장하는 것은 맨 아래 InputGuardrailMiddleware 하나뿐입니다.
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from logging_middleware import LoggingMiddleware                         # 2-1
from masking_middleware import MaskingMiddleware                         # 2-2
from guards_input import input_guard, rule_check, llm_check              # 4-2
from guards_refusal import REFUSAL_MESSAGES, refusal_message, log_block  # 4-3
from guards_output import (                                              # 5-1
    OutputGuardrailMiddleware,
    get_text,
    mask_pii,
    has_pii,
    is_off_topic,
    check_grounding,
)

class InputGuardrailMiddleware(AgentMiddleware):
    """4-2의 input_guard를 미들웨어로 감싼 것. 주입이면 모델 호출 자체를 생략합니다."""

    def _refusal_or_none(self, request):
        """차단할 입력이면 거절 메시지를, 통과할 입력이면 None을 돌려줍니다."""
        last_human = next(
            (m for m in reversed(request.state["messages"])
             if isinstance(m, HumanMessage)), None)
        if last_human:
            blocked, reason = input_guard(get_text(last_human))
            if blocked:
                print(f"[guard] 입력 차단: {reason}")   # 사유는 내부 기록만
                return refusal_message(reason)
        return None

    def wrap_model_call(self, request, handler):
        refusal = self._refusal_or_none(request)
        if refusal is not None:
            return refusal
        return handler(request)

    async def awrap_model_call(self, request, handler):
        # MCP 도구가 비동기라 5-2 종합 실습은 ainvoke로 돕니다. 그때는 이쪽이 불립니다
        refusal = self._refusal_or_none(request)
        if refusal is not None:
            return refusal
        return await handler(request)