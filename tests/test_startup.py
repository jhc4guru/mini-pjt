"""시작 점검(D-54): 필수 CLI가 없으면 시작을 거부하고, 루프백이 아닌 바인딩은 명시적으로 허용했을 때만 연다. Docker 파일이 비밀값을 이미지에 넣지 않는다."""
import re
from pathlib import Path

import pytest

from app.web import startup
from app.web.startup import check_bind, check_cli_tools, is_loopback

ROOT = Path(__file__).resolve().parent.parent


def runner(versions):
    def run(cmd):
        out = versions.get(cmd[0])
        if out is None:
            raise FileNotFoundError(cmd[0])
        return (0 if out else 1), out
    return run


def test_missing_or_old_cli_tools_are_reported_with_install_hints():
    ok = check_cli_tools(which=lambda n: f"/usr/bin/{n}", run=runner({"aws": "aws-cli/2.39.3 Python/3.14", "kubectl": "Client Version: v1.37.2"}))
    assert ok == []
    none = check_cli_tools(which=lambda n: None)
    assert len(none) == 2 and "aws" in none[0] and "kubectl" in none[1] and "설치" in none[0]
    v1 = check_cli_tools(which=lambda n: f"/usr/bin/{n}", run=runner({"aws": "aws-cli/1.29.0 Python/3.9", "kubectl": "Client Version: v1.30"}))
    assert len(v1) == 1 and "AWS CLI v2가 필요합니다" in v1[0]
    broken = check_cli_tools(which=lambda n: f"/usr/bin/{n}", run=runner({"aws": "aws-cli/2.1.0", "kubectl": ""}))
    assert len(broken) == 1 and "kubectl version --client" in broken[0]


def test_non_loopback_binding_needs_an_explicit_allow():
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost") and not is_loopback("0.0.0.0") and not is_loopback("192.168.0.5")
    assert check_bind("127.0.0.1", {}) is None
    assert "ALLOW_NON_LOOPBACK=1" in check_bind("0.0.0.0", {})
    assert check_bind("0.0.0.0", {"ALLOW_NON_LOOPBACK": "1"}) is None and check_bind("0.0.0.0", {"ALLOW_NON_LOOPBACK": "0"}) is not None


def test_main_refuses_to_start_when_a_tool_is_missing_or_the_bind_is_open(monkeypatch):
    from app.web import main as m
    monkeypatch.setattr("sys.argv", ["main", "--host", "0.0.0.0"])
    monkeypatch.delenv("ALLOW_NON_LOOPBACK", raising=False)
    with pytest.raises(SystemExit) as e:
        m.main()
    assert "루프백이 아닙니다" in str(e.value)
    monkeypatch.setattr("sys.argv", ["main"])
    monkeypatch.setattr(m, "check_cli_tools", lambda: ["`aws` CLI를 찾을 수 없습니다."])
    with pytest.raises(SystemExit) as e:
        m.main()
    assert "필수 도구가 없습니다" in str(e.value)                                  # build_service가 호출되기 전에 멈춘다


# ---------------- Docker 파일 ----------------
def test_the_image_never_contains_secrets_or_local_state_and_runs_as_a_non_root_user():
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for must in (".env", ".venv/", "data/", "eval/reports/"):
        assert must in ignore, must
    df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert not re.search(r"^\s*COPY\s.*\.env", df, re.M) and "ENV AWS_" not in df and "ARG AWS_" not in df          # 자격증명을 굽지 않는다
    assert re.search(r"^USER app", df, re.M) and "ALLOW_NON_LOOPBACK=1" in df and "sha256sum -c" in df                # 비root, 명시 허용, kubectl 체크섬
    assert "127.0.0.1:8080:8000" in df                                                                               # 실행 안내가 호스트 쪽을 루프백으로만 연다
    assert "requirements-runtime.txt" in df
    assert "groff-base" in df and "assert not missing" in df                                                         # aws help 렌더러(groff)와 스냅샷 완전성 확인(없으면 빌드 실패)


def test_runtime_requirements_cover_every_third_party_import_the_app_makes():
    req = {re.split(r"[=\[<>]", l.strip())[0].lower().replace("_", "-") for l in (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")}
    imports = set()
    for p in (ROOT / "app").rglob("*.py"):
        imports |= set(re.findall(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", p.read_text(encoding="utf-8"), re.M))
    third = {"boto3": "boto3", "botocore": "boto3", "chromadb": "chromadb", "cryptography": "cryptography", "dotenv": "python-dotenv", "fastapi": "fastapi", "kiwipiepy": "kiwipiepy",
             "pydantic": "pydantic", "rank_bm25": "rank-bm25", "uvicorn": "uvicorn", "yaml": "pyyaml"}
    for mod, pkg in third.items():
        if mod in imports:
            assert pkg in req, (mod, pkg)
    assert "moto" in req                                                                                               # 모의 서버는 서브프로세스로 실행돼 import에 안 보인다
