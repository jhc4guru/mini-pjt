"""화면(단일 HTML): 서버가 주는 파일이 안전하고, 화면이 보내는 메시지가 서비스가 받는 메시지와 맞는지 정적으로 확인한다.

브라우저에서 실제로 그려 보는 확인은 이 테스트가 하지 않는다(README의 "미확인" 참고).
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"
SERVICE = Path(__file__).resolve().parent.parent / "app" / "session" / "service.py"


@pytest.fixture(scope="module")
def html():
    return INDEX.read_text(encoding="utf-8")


def script_of(html: str) -> str:
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m
    return m.group(1)


def test_script_has_valid_syntax(html, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node가 필요합니다")
    f = tmp_path / "ui.js"
    f.write_text(script_of(html), encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:500]


def test_the_page_never_puts_data_into_html(html):
    """모델 출력·실행 결과에 HTML이 있어도 해석되지 않도록 DOM은 textContent(텍스트 노드)로만 만든다."""
    js = script_of(html)
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "srcdoc"):
        assert bad not in js, bad
    assert "http://" not in html and "https://" not in re.sub(r'location\.protocol === "https:"', "", html)       # 외부 리소스를 불러오지 않는다


def test_every_message_the_page_sends_is_accepted_by_the_service(html):
    sent = set(re.findall(r'type:"([a-z_]+)"', script_of(html))) - {"auth", "checkbox"}
    accepted = set(re.findall(r't == "([a-z_]+)"', SERVICE.read_text(encoding="utf-8")))
    assert sent <= accepted, sent - accepted
    ops_sent = set(re.findall(r'op:"([a-z_]+)"', script_of(html)))
    ops_accepted = set(re.findall(r'op == "([a-z_]+)"', SERVICE.read_text(encoding="utf-8")))
    assert ops_sent <= ops_accepted, ops_sent - ops_accepted


def test_the_page_can_send_every_approval_action(html):
    """승인 흐름의 동작이 화면에서 빠지지 않았는지(서버 동작 목록과 대조). 화면이 쓰지 않는 서버 동작은 의도한 것만 허용한다."""
    sent = set(re.findall(r'type:"([a-z_]+)"', script_of(html))) - {"checkbox"}
    accepted = set(re.findall(r't == "([a-z_]+)"', SERVICE.read_text(encoding="utf-8")))
    assert accepted - sent == set(), accepted - sent


def test_every_server_flag_and_requirement_has_a_korean_label(html):
    from app.guards.command_guard import HIGH_FLAGS
    js = script_of(html)
    for flag in HIGH_FLAGS:
        assert re.search(rf"\b{flag}:", js), flag
    for req in ("open_cidr_confirm", "strong_warning", "dry_run_first", "prod_warning", "fallback_approval", "show_file_contents"):
        assert f"{req}:" in js, req


def test_state_labels_cover_every_session_and_step_state(html):
    from app.session.states import SESSION_TRANSITIONS, STEP_TRANSITIONS
    js = script_of(html)
    for s in (*SESSION_TRANSITIONS, *STEP_TRANSITIONS):
        assert re.search(rf"\b{s}:\"", js), s


def test_the_server_serves_the_page_with_a_restrictive_policy(tmp_path):
    from starlette.testclient import TestClient

    from app.web.server import create_app

    class Dummy:
        listeners = []

        def recover_all(self):
            return {}

        def purge_expired_mappings(self):
            return 0

    with TestClient(create_app(Dummy())) as c:
        r = c.get("/")
        assert r.status_code == 200 and "<title>DevOps Plan/Apply</title>" in r.text
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert r.headers["x-content-type-options"] == "nosniff"


def test_the_review_is_folded_by_default(html):
    """사용자 요청: 리뷰는 눈에 잘 안 들어오므로 기본으로 접는다(위험 등급은 헤더에 이미 보인다)."""
    js = script_of(html)
    start = js.index("function reviewPanel")
    body = js[start:js.index("const legend")]
    assert 'h("details", {class:"card"}, kids)' in body and "open:" not in body


def test_steps_that_use_aws_eks_carry_a_badge_for_review(html):
    js = script_of(html)
    assert '(s.command || "").startsWith("aws eks")' in js and 'h("span", {class:"badge b-warn"' in js[js.index('startsWith("aws eks")'):js.index('startsWith("aws eks")') + 200]
