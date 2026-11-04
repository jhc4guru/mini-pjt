"""한국어 + 영문 CLI 토큰을 함께 다루는 BM25용 토크나이저.

- 플레이스홀더(<SG_1> 등)는 버린다(모든 문서에 있어 변별력이 없다).
- 하이픈 복합어(create-security-group, --group-id)는 통째 토큰과 부분 토큰을 모두 남긴다.
- 한국어는 kiwipiepy 형태소 중 명사·어간·영문·숫자만 남긴다.
"""
from __future__ import annotations

import re
from functools import lru_cache

_KEEP = {"NNG", "NNP", "NNB", "NR", "NP", "VV", "VA", "SL", "SN", "XR"}
_STOP = {"하", "있", "되", "않", "같", "수", "것", "등", "및", "때", "더"}
_PLACEHOLDER = re.compile(r"<[A-Z]+(?:_[A-Z]+)*(?:_\d+)?>")
_COMPOUND = re.compile(r"--?[a-z0-9]+(?:-[a-z0-9]+)*|[a-z0-9]+(?:-[a-z0-9]+)+")


@lru_cache(maxsize=1)
def _kiwi():
    from kiwipiepy import Kiwi

    return Kiwi()


def tokenize(text: str) -> list[str]:
    t = _PLACEHOLDER.sub(" ", text).lower()
    toks: list[str] = []
    toks.extend(m.group(0) for m in _COMPOUND.finditer(t))  # create-security-group, --group-id 통째 토큰
    # 형태소 분석 입력에서는 하이픈을 공백으로 바꿔 부분 토큰이 나오게 한다
    t2 = _COMPOUND.sub(lambda m: m.group(0).replace("-", " "), t)
    for tok in _kiwi().tokenize(t2):
        if tok.tag in _KEEP and tok.form not in _STOP:
            toks.append(tok.form)
    return toks
