"""문서 사이의 일관성 검사.

스택 이름은 두 곳에 있다: CLAUDE.md의 "스택 제약" 한 줄(동작을 정하는 규칙)과 SERVICE.md 1-1절의 표(사용 중/예정 현황).
스택이 바뀌면 두 곳을 함께 고쳐야 하고, 이 테스트가 CLAUDE.md 쪽 이름이 SERVICE.md에 없을 때를 잡는다.
(반대 방향, 즉 SERVICE.md에 새 기술을 넣고 CLAUDE.md 제약 줄을 안 고친 경우는 사람이 챙겨야 한다. 규칙은 CLAUDE.md '문서 작성 규칙'.)
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def _service_stack_section() -> str:
    t = _read("SERVICE.md")
    a = t.index("## 1-1. 기술 스택")
    b = t.index("\n## ", a + 5)
    return t[a:b]


def test_stack_names_in_claude_constraint_exist_in_service_stack_section():
    m = re.search(r"\*\*스택은 정해진 범위 안에서 쓴다:\*\*(.*?)새 프레임워크", _read("CLAUDE.md"), re.S)
    assert m, "CLAUDE.md에서 스택 제약 줄을 찾지 못했다(줄을 지웠거나 문구를 바꿨다면 이 테스트도 함께 고친다)"
    names = set(re.findall(r"[A-Za-z][A-Za-z0-9/]*", m.group(1)))
    assert len(names) >= 5, f"스택 이름을 거의 못 읽었다: {names}"
    service = _service_stack_section().lower()
    missing = sorted(n for n in names if n.lower() not in service)
    assert not missing, f"CLAUDE.md 스택 제약에 있는 {missing}이(가) SERVICE.md 1-1절에 없다. 두 곳을 함께 고친다."


def test_claude_md_points_to_the_stack_section_that_exists():
    assert "## 1-1. 기술 스택" in _read("SERVICE.md")
    assert "1-1절" in _read("CLAUDE.md")
