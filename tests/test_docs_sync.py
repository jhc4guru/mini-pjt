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


def test_no_doc_example_uses_dollar_signs_the_command_guard_would_block():
    """문서 예시가 명령 가드의 환경변수 확장 차단(`$`)에 걸리면 모델이 문서를 따라 쓴 명령이 막힌다."""
    from app.rag.chunks import load_chunks
    for c in load_chunks():
        for line in c.text.splitlines():
            if line.lstrip().startswith(("- 예시", "예시")) or "`aws " in line or "`kubectl " in line:
                for cmd in __import__("re").findall(r"`((?:aws|kubectl) [^`]*)`", line):
                    assert "$" not in cmd, (c.id, cmd)


def test_no_doc_chunk_contains_values_the_leak_scanner_flags():
    """검색 문서 텍스트는 리랭크 등 외부 모델 경계로 그대로 나간다. 문서에 IP·ID 같은 값이 있으면 누출 검사에 걸린다(예시는 플레이스홀더로)."""
    from app.guards.patterns import find_sensitive
    from app.rag.chunks import load_chunks
    for c in load_chunks():
        assert not find_sensitive(c.text), (c.id, find_sensitive(c.text)[0][0])
