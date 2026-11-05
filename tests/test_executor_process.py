"""실행 환경 격리: 서버 환경변수 비상속, 요청별 임시 HOME·kubeconfig, 타임아웃, 출력 상한."""
import json
import sys
from pathlib import Path

import pytest

from app.executor.moto_server import assert_local_endpoint
from app.executor.process import ArgvError, isolated_env, run_cli, tokenize

PROBE = (
    "import json, os, sys\n"
    "kc = os.environ.get('KUBECONFIG', '')\n"
    "print(json.dumps({'env': dict(os.environ), 'cwd': os.getcwd(), 'kube_exists': os.path.exists(kc), 'argv': sys.argv[1:]}))\n"
)
SLEEP = "import time\ntime.sleep(30)\n"
BIG = "import sys\nsys.stdout.write('a' * 1_200_000)\n"


def binaries(code):
    return {"aws": [sys.executable, "-c", code]}


@pytest.fixture()
def poisoned(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "AKIAREALLOOKINGKEY00", "AWS_SECRET_ACCESS_KEY": "realsecret", "AWS_PROFILE": "prod",
                 "AWS_SESSION_TOKEN": "tok", "KUBECONFIG": "/home/real/.kube/config", "LANGFUSE_SECRET_KEY": "lf-secret",
                 "AWS_ENDPOINT_URL": "https://real.example", "MAIN_MODEL": "m"}.items():
        monkeypatch.setenv(k, v)


def test_isolated_env_does_not_inherit_server_environment(poisoned, tmp_path):
    env = isolated_env(tmp_path, {"AWS_ENDPOINT_URL": "http://127.0.0.1:1", "AWS_ACCESS_KEY_ID": "testing"})
    assert env["AWS_ACCESS_KEY_ID"] == "testing" and env["AWS_ENDPOINT_URL"] == "http://127.0.0.1:1"
    for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "LANGFUSE_SECRET_KEY", "MAIN_MODEL", "AWS_SECRET_ACCESS_KEY"):
        assert k not in env, k
    assert env["KUBECONFIG"].startswith(str(tmp_path)) and env["HOME"] == str(tmp_path)
    assert env["AWS_CONFIG_FILE"].startswith(str(tmp_path)) and env["AWS_SHARED_CREDENTIALS_FILE"].startswith(str(tmp_path))


def test_child_process_sees_only_the_isolated_environment(poisoned, tmp_path):
    out = run_cli("aws version", workdir=tmp_path, extra_env={"AWS_ACCESS_KEY_ID": "testing"}, timeout=30, binaries=binaries(PROBE))
    assert out.status == "ok"
    seen = json.loads(out.stdout)
    assert seen["env"]["AWS_ACCESS_KEY_ID"] == "testing"
    for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECRET_ACCESS_KEY", "LANGFUSE_SECRET_KEY", "MAIN_MODEL"):
        assert k not in seen["env"], k
    assert seen["env"]["KUBECONFIG"] != "/home/real/.kube/config" and "exec-" in seen["env"]["KUBECONFIG"]
    assert seen["kube_exists"] is True                                      # 실행 중에는 요청별 kubeconfig가 있다
    assert not Path(seen["env"]["KUBECONFIG"]).exists()                      # 끝나면 지워진다
    assert Path(seen["cwd"]).resolve() == tmp_path.resolve() and seen["argv"] == ["version"]


def test_each_request_gets_its_own_temp_home(tmp_path):
    homes = {json.loads(run_cli("aws x", workdir=tmp_path, extra_env=None, timeout=30, binaries=binaries(PROBE)).stdout)["env"]["HOME"] for _ in range(2)}
    assert len(homes) == 2


def test_timeout_kills_the_process_and_reports_it(tmp_path):
    out = run_cli("aws x", workdir=tmp_path, extra_env=None, timeout=1, binaries=binaries(SLEEP))
    assert out.status == "timeout" and out.exit_code is None and out.duration < 10


def test_output_is_capped(tmp_path):
    out = run_cli("aws x", workdir=tmp_path, extra_env=None, timeout=30, binaries=binaries(BIG))
    assert len(out.stdout) == 1_000_000 and "output_truncated" in out.notes


def test_missing_cli_is_reported_not_raised(tmp_path):
    out = run_cli("aws x", workdir=tmp_path, extra_env=None, timeout=5, binaries={"aws": ["definitely-not-a-real-binary-xyz"]})
    assert out.status == "failed" and "cli_unavailable" in out.notes


@pytest.mark.parametrize("cmd", ["aws a; rm -rf /", "aws a | cat", "aws a $(whoami)", "aws a\nkubectl b", "aws 'unbalanced"])
def test_unsafe_commands_are_not_tokenized(cmd):
    with pytest.raises(ArgvError):
        tokenize(cmd)


def test_tokenization_is_the_guards(tmp_path):
    assert tokenize("aws ec2 describe-instances --filters Name=tag:Env,Values=dev") == ["aws", "ec2", "describe-instances", "--filters", "Name=tag:Env,Values=dev"]


@pytest.mark.parametrize("url", ["https://iam.amazonaws.com", "http://169.254.169.254", "http://example.com:5000", "http://127.0.0.1.evil.com", "https://127.0.0.1:5000"])
def test_only_loopback_http_endpoints_are_allowed_for_mock(url):
    with pytest.raises(ValueError):
        assert_local_endpoint(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:5000", "http://localhost:5000"])
def test_loopback_endpoint_is_allowed(url):
    assert_local_endpoint(url)
