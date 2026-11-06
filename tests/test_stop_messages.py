"""중단 안내(D-44): 원인별 한국어 안내가 있고, 작업을 나눠 요청하라는 문구는 없다(D-63: 나눠도 사용량이 줄지 않아 안내를 뺐다)."""
import pytest

from app.planner.pipeline import stop_message

CODES = ["repeated_call", "tool_calls_exceeded", "steps_exceeded", "tokens_exceeded", "time_exceeded", "llm_unavailable",
         "output_truncated", "no_submission", "repair_exhausted", "plan_too_large", "leak_blocked"]


@pytest.mark.parametrize("code", CODES)
def test_every_stop_has_guidance_without_split_advice(code):
    msg = stop_message(code)
    assert msg and "나눠서" not in msg and "사전 작업" not in msg


def test_detail_is_still_appended_for_unavailable_models():
    assert "(원인: MAIN 호출 실패" in stop_message("llm_unavailable", ("MAIN 호출 실패: ThrottlingException",))
