"""입력 가드: 사용자 입력이 모델 경계를 넘기 전에 검사하고 마스킹한다. 하나라도 걸리면 차단한다(fail-closed).

반환값의 reasons는 코드뿐이고 입력 값은 담지 않는다. 사용자에게는 user_message()로 값을 되풀이하지 않는 안내를 준다.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.guards.masking import Masker
from app.guards.patterns import find_injection, find_secrets, find_sensitive

MAX_INPUT_CHARS = 2000
_ZERO_WIDTH = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class InputVerdict:
    allow: bool
    masked_text: str | None
    reasons: tuple[str, ...] = ()
    masked_counts: dict = field(default_factory=dict, compare=False)


def _normalize_for_scan(text: str) -> str:
    """전각·호환 문자와 보이지 않는 문자로 패턴을 피하는 우회를 막기 위한 검사용 정규화."""
    return _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))


def check_user_input(text: object, masker: Masker, *, max_chars: int = MAX_INPUT_CHARS) -> InputVerdict:
    if not isinstance(text, str) or not text.strip():
        return InputVerdict(False, None, ("empty",))
    if len(text) > max_chars:
        return InputVerdict(False, None, ("too_long",))
    if _CONTROL.search(text):
        return InputVerdict(False, None, ("control_chars",))

    scan = _normalize_for_scan(text)
    reasons = [f"prompt_injection:{k}" for k in find_injection(scan)]
    reasons += [f"secret_in_input:{k}" for k in find_secrets(scan)]
    if reasons:
        return InputVerdict(False, None, tuple(reasons))

    masked, report = masker.mask_with_report(scan)
    # 방어적 재검사: 마스킹 후에도 민감값이 남았다면 막는다(마스킹 누락은 곧 외부 유출이다).
    if masker.leaked_kinds(masked) or find_sensitive(masked):
        return InputVerdict(False, None, ("mask_failure",))
    return InputVerdict(True, masked, (), report.counts)


_MESSAGES = {
    "empty": "요청 내용이 비어 있습니다.",
    "too_long": "요청이 너무 깁니다. 나눠서 입력해 주세요.",
    "control_chars": "허용되지 않는 문자가 포함되어 있습니다.",
    "prompt_injection": "시스템 지시를 바꾸거나 승인·가드를 우회하려는 표현이 포함되어 있어 처리할 수 없습니다.",
    "secret_in_input": "비밀번호, 키, 토큰 같은 비밀값은 입력할 수 없습니다. 비밀값은 본인 터미널에서 직접 입력해 주세요.",
    "mask_failure": "민감값 마스킹을 확인하지 못해 요청을 처리할 수 없습니다.",
}


def user_message(reasons: tuple[str, ...]) -> str:
    """사용자에게 보여줄 안내. 입력 값이나 어떤 패턴에 걸렸는지는 알리지 않는다."""
    seen: list[str] = []
    for r in reasons:
        msg = _MESSAGES.get(r.split(":", 1)[0], "요청을 처리할 수 없습니다.")
        if msg not in seen:
            seen.append(msg)
    return " ".join(seen)
