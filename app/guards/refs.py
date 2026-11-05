"""앞 절차의 결과 참조(D-48): 명령에 `@{절차id.필드경로}`를 적으면 서버가 실행 직전에 그 절차의 (마스킹된) 출력에서 리소스 ID만 꺼내 바꿔 넣는다.

- 승인되는 것은 참조가 적힌 템플릿이다(plan 해시에 들어간다). 실행되는 것은 "같은 템플릿 + 서버가 검증한 리소스 ID 치환"이다.
- 치환 값은 **리소스 ID 모양**(`sg-…`, `subnet-…`, `i-…` 등)이어야 한다. 이름·ARN·IP·임의 문자열은 거부한다(셸 문법이나 지시가 섞일 수 없다).
- 치환은 저장된 마스킹 출력에서 한다. 값은 플레이스홀더(`<SG_1>`)로 들어가고 실제 값 복원은 기존 경로(`Masker.unmask`)만 쓴다.
- 치환한 명령은 호출하는 쪽이 `CommandGuard.check`로 다시 검사하고 실행기 관문(`Executor.execute`)을 통과해야 한다. 참조가 남은 명령은 실행기가 거부한다.
- 참조는 서버가 실행하는 읽기·변경 절차(`cli`·`poll`)에서만, 앞서 서버가 실행한 절차의 출력에서만 쓴다. 에이전트의 읽기 도구·미리보기·사람이 실행하는 절차에서는 쓰지 않는다.
"""
from __future__ import annotations

import re

_SEG = r"[A-Za-z][A-Za-z0-9]*(?:\[\d{1,3}\])?"
REF_RE = re.compile(r"@\{([A-Za-z0-9][A-Za-z0-9_-]{0,63})\.(" + _SEG + r"(?:\." + _SEG + r"){0,5})\}")
DUMMY_ID = "ref-0000000000000000"                      # 가드가 템플릿을 검사할 때 참조 자리에 넣는 리소스 ID 모양의 가짜 값
MAX_REFS = 4


def find_refs(command: str | None) -> list[tuple[str, str]]:
    """명령 안의 참조 [(절차 id, 필드 경로)]를 나온 순서대로 돌려준다."""
    if not isinstance(command, str) or "@{" not in command:
        return []
    return [(m.group(1), m.group(2)) for m in REF_RE.finditer(command)]


def has_unparsed_ref(command: str | None) -> bool:
    """`@{`가 있는데 문법에 맞지 않는 참조가 섞여 있는가(그대로 실행되면 안 된다)."""
    if not isinstance(command, str) or "@{" not in command:
        return False
    return command.count("@{") != len(find_refs(command))


_NOT_JSON = re.compile(r"--query\b|--output[ =](?:text|table|yaml|yaml-stream)\b")


def output_not_json(command: str | None) -> bool:
    """참조되는 절차의 명령이 출력을 JSON이 아니게 만드는가(`--query`, `--output text|table|yaml`). 그러면 실행 때 참조를 풀 수 없다."""
    return bool(command and _NOT_JSON.search(command))


def with_dummy(command: str) -> str:
    """가드 검사용: 참조 자리에 리소스 ID 모양의 가짜 값을 넣는다."""
    return REF_RE.sub(DUMMY_ID, command)
