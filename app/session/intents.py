"""대화로 하는 진행 요청("apply 진행해줘", "다음 절차 실행해줘", "확정해줘")을 **실행 제안**으로 바꾼다(D-49).

- 모델을 쓰지 않는다. 정해진 낱말 규칙으로 의도를 고르고, 지금 세션 상태에서 할 수 있는 동작 하나를 제안 카드(`action`)로 만든다.
- 카드는 아무것도 실행하지 않는다. 사람이 카드를 눌렀을 때에만 같은 이름의 정식 메시지(`approve_plan`, `start_apply`, `approve_step`, `run_step`, `run_read`, `send_to_apply`)가
  **누른 사람의 신원**으로 처리되고, 요청자 제한·해시·승인 자격·가드 같은 서버 검사를 그대로 거친다(대화로 우회할 수 없다).
- 연습장을 고치는 말(지워·바꿔·추가·옮겨 등)은 여기서 다루지 않고 말로 수정(모델 수정안)으로 간다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

EDIT_WORDS = re.compile(r"지워|지우|삭제|바꿔|바꾸|고쳐|고치|수정|추가|옮겨|옮기|복제|이름|제목|끼워|넣어|변경")
_APPLY = re.compile(r"apply|어플라이|적용")
_START = re.compile(r"진행|시작|해\s*줘|해줘|하자|가자|넘어|들어가")
_REVIEW = re.compile(r"검토\s*요청|리뷰\s*요청|(plan|플랜|계획).{0,6}(올려|보내|제출|요청)")
_APPROVE = re.compile(r"확정|승인|ok|오케이|허락")
_PLAN = re.compile(r"plan|플랜|계획|전체|일괄")
_RUN = re.compile(r"실행|돌려|run|시작해|확인\s*해")
_REJECT = re.compile(r"반려|재검토|다시\s*검토|돌려\s*보내|수정\s*요청")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")


@dataclass(frozen=True)
class Intent:
    kind: str                 # start_apply | send_review | approve_plan | reject_plan | approve_step | run_step
    step_hint: str | None = None   # 말에 나온 절차 id(있으면)


def parse_intent(text: str, step_ids: set[str] | frozenset[str] = frozenset()) -> Intent | None:
    """진행 요청이면 의도를, 아니면 None(→ 말로 수정으로 간다)."""
    t = (text or "").strip().lower()
    if t and _REJECT.search(t):
        return Intent("reject_plan")                                                  # 말 전체가 반려 코멘트가 된다(카드를 눌러야 반려된다)
    if not t or EDIT_WORDS.search(t):
        return None
    hint = next((i for i in _ID.findall(text or "") if i in step_ids), None)
    if _REVIEW.search(t):
        return Intent("send_review")
    if _APPLY.search(t) and _START.search(t) and not hint:
        return Intent("start_apply")
    if _APPROVE.search(t):
        return Intent("approve_plan" if (_PLAN.search(t) or "확정" in t) and not hint else "approve_step", hint)       # "확정"은 plan 확정(일괄 승인)을 뜻한다
    if _RUN.search(t):
        return Intent("run_step", hint)
    return None
