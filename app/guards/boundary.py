"""외부 모델 경계(LLM, 임베딩, 리랭크, 트레이스 export) 직전 누출 검사.

마스킹이 끝났어야 할 텍스트에 민감값(패턴 또는 이 세션이 아는 실제 값)이 남아 있으면 호출하지 않고 막는다(fail-closed).
오류 메시지에는 값이 아니라 종류만 담는다.
"""
from __future__ import annotations

from app.guards.masking import Masker
from app.guards.patterns import find_secrets


class LeakError(RuntimeError):
    def __init__(self, where: str, kinds: list[str]):
        super().__init__(f"{where}: 민감값 감지({', '.join(kinds)})")
        self.where = where
        self.kinds = kinds


def leak_kinds(text: str, masker: Masker) -> list[str]:
    kinds = set(masker.leaked_kinds(text))
    kinds.update(f"secret:{k}" for k in find_secrets(text))
    return sorted(kinds)


def assert_outbound_clean(text: str, masker: Masker, where: str) -> None:
    """모델·트레이스로 보내기 직전에 호출한다. 누출이 있으면 LeakError."""
    kinds = leak_kinds(text, masker)
    if kinds:
        raise LeakError(where, kinds)
