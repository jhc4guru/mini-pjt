"""앞 절차의 결과 참조의 치환(D-48, 문법과 가짜 값 검사는 `refs.py`): 명령에 `@{절차id.필드경로}`를 적으면 서버가 실행 직전에 그 절차의 (마스킹된) 출력에서 리소스 ID만 꺼내 바꿔 넣는다.

- 승인되는 것은 참조가 적힌 템플릿이다(plan 해시에 들어간다). 실행되는 것은 "같은 템플릿 + 서버가 검증한 리소스 ID 치환"이다.
- 치환 값은 **리소스 ID 모양**(`sg-…`, `subnet-…`, `i-…` 등)이어야 한다. 이름·ARN·IP·임의 문자열은 거부한다(셸 문법이나 지시가 섞일 수 없다).
- 치환은 저장된 마스킹 출력에서 한다. 값은 플레이스홀더(`<SG_1>`)로 들어가고 실제 값 복원은 기존 경로(`Masker.unmask`)만 쓴다.
- 치환한 명령은 호출하는 쪽이 `CommandGuard.check`로 다시 검사하고 실행기 관문(`Executor.execute`)을 통과해야 한다. 참조가 남은 명령은 실행기가 거부한다.
- 참조는 서버가 실행하는 읽기·변경 절차(`cli`·`poll`)에서만, 앞서 서버가 실행한 절차의 출력에서만 쓴다. 에이전트의 읽기 도구·미리보기·사람이 실행하는 절차에서는 쓰지 않는다.
"""
from __future__ import annotations

import json
import re
from typing import Callable

from app.guards.execution_policy import ExecutionDenied
from app.guards.masking import PLACEHOLDER_RE, Masker
from app.guards.patterns import SENSITIVE_PATTERNS
from app.guards.refs import MAX_REFS, REF_RE, find_refs, has_unparsed_ref

_RESOURCE_ID = SENSITIVE_PATTERNS["resource_id"]


class RefError(ExecutionDenied):
    """참조를 치환하지 못했다. 코드는 `ref:<이유>`이고 실행기에 닿기 전에 실행을 막는다."""

    def __init__(self, reason: str):
        super().__init__(f"ref:{reason}")


def _walk(data, path: str):
    cur = data
    for seg in path.split("."):
        m = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*)(?:\[(\d{1,3})\])?", seg)
        if not m or not isinstance(cur, dict) or m.group(1) not in cur:
            return None
        cur = cur[m.group(1)]
        if m.group(2) is not None:
            i = int(m.group(2))
            if not isinstance(cur, list) or i >= len(cur):
                return None
            cur = cur[i]
    return cur


def _json_of(text: str):
    t = (text or "").strip()
    try:
        return json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a != -1 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except ValueError:
                return None
    return None


def extract_id(text: str, path: str, masker: Masker) -> str:
    """저장된 (마스킹된) 출력 JSON에서 경로의 값을 꺼내 리소스 ID 플레이스홀더로 돌려준다. 리소스 ID가 아니면 `RefError`."""
    value = _walk(_json_of(text), path)
    if value is None:
        raise RefError("path_not_found")
    if not isinstance(value, str):
        raise RefError("value_not_resource_id")
    if PLACEHOLDER_RE.fullmatch(value):
        if masker.unresolved(value) or not _RESOURCE_ID.fullmatch(masker.unmask(value)):
            raise RefError("value_not_resource_id")
        return value
    if not _RESOURCE_ID.fullmatch(value):
        raise RefError("value_not_resource_id")
    masked = masker.mask(value)
    if not PLACEHOLDER_RE.fullmatch(masked):
        raise RefError("value_not_resource_id")
    return masked


def resolve(command: str, output_of: Callable[[str], str], masker: Masker) -> str:
    """참조를 모두 치환한 (마스킹된) 명령을 돌려준다. `output_of(절차 id)`는 그 절차의 성공한 마지막 출력 텍스트이며 없으면 `RefError`를 던져야 한다."""
    refs = find_refs(command)
    if has_unparsed_ref(command) or len(refs) > MAX_REFS:
        raise RefError("bad_reference")
    cache: dict[str, str] = {}

    def sub(m: re.Match) -> str:
        step_id, path = m.group(1), m.group(2)
        if step_id not in cache:
            cache[step_id] = output_of(step_id)
        return extract_id(cache[step_id], path, masker)
    return REF_RE.sub(sub, command)
