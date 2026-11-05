"""CLI 프로세스 실행(셸 없음)과 격리된 환경 구성.

- 환경은 비어 있는 상태에서 필요한 것만 채운다. 서버의 환경변수(`.env`의 실제 AWS 키, 프로필, kubeconfig, Langfuse 키 등)를 상속하지 않는다.
- HOME과 AWS 설정 파일 경로는 요청별 임시 폴더로 돌려서 사용자의 `~/.aws`, `~/.kube`를 읽지도 쓰지도 않는다. KUBECONFIG도 요청별 임시 파일이다.
- 인자는 명령 가드와 같은 토큰화(`command_guard._split`)로 나눈다. 가드가 본 것과 실행되는 것이 같다.
- 전체 타임아웃을 넘기면 프로세스를 죽이고, 출력은 길이 상한에서 자른다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.executor.base import MAX_OUTPUT_CHARS
from app.guards.command_guard import _split

_KUBECONFIG_STUB = "apiVersion: v1\nkind: Config\nclusters: []\ncontexts: []\nusers: []\ncurrent-context: ''\n"


class ArgvError(ValueError):
    """명령을 안전하게 토큰으로 나눌 수 없다."""


@dataclass
class ProcessOutcome:
    status: str                      # ok | failed | timeout
    exit_code: int | None
    stdout: str
    stderr: str
    duration: float
    notes: list[str] = field(default_factory=list)


def isolated_env(tmp: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """상속 없이 새로 만든 환경. `extra`(엔드포인트, 더미 키 등)만 더한다."""
    env = {
        "PATH": os.environ.get("PATH", ""),                   # aws·kubectl 실행 파일을 찾는 데만 쓴다
        "HOME": str(tmp), "USERPROFILE": str(tmp), "TMPDIR": str(tmp), "TEMP": str(tmp), "TMP": str(tmp),
        "AWS_CONFIG_FILE": str(tmp / "aws-config"), "AWS_SHARED_CREDENTIALS_FILE": str(tmp / "aws-credentials"),
        "AWS_PAGER": "", "AWS_EC2_METADATA_DISABLED": "true", "AWS_SDK_LOAD_CONFIG": "0",
        "KUBECONFIG": str(tmp / "kubeconfig"), "PYTHONIOENCODING": "utf-8",
    }
    if os.name == "nt":                                         # Windows는 이 값이 없으면 실행 파일이 뜨지 않는다
        for k in ("SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR"):
            if k in os.environ:
                env[k] = os.environ[k]
    env.update(extra or {})
    return env


def tokenize(command: str) -> list[str]:
    tokens, blocks = _split(command)
    if blocks or not tokens:
        raise ArgvError("명령을 토큰으로 나눌 수 없다")
    return tokens


def run_cli(command: str, *, workdir: Path, extra_env: dict[str, str] | None, timeout: float,
            binaries: dict[str, list[str]] | None = None) -> ProcessOutcome:
    """aws/kubectl 명령 하나를 실행한다. 요청마다 새 임시 폴더(HOME, 설정, kubeconfig)를 만들고 끝나면 지운다."""
    argv = tokenize(command)
    cli = argv[0]
    exe = (binaries or {}).get(cli) or [shutil.which(cli) or cli]
    full = [*exe, *argv[1:]]
    with tempfile.TemporaryDirectory(prefix="exec-") as t:
        tmp = Path(t)
        (tmp / "kubeconfig").write_text(_KUBECONFIG_STUB, encoding="utf-8")
        t0 = time.perf_counter()
        try:
            p = subprocess.run(full, cwd=str(workdir), env=isolated_env(tmp, extra_env), stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return ProcessOutcome("timeout", None, "", "", time.perf_counter() - t0, ["timeout"])
        except FileNotFoundError:
            return ProcessOutcome("failed", None, "", "", time.perf_counter() - t0, ["cli_unavailable"])
        dt = time.perf_counter() - t0
        out = p.stdout.decode("utf-8", errors="replace")
        err = p.stderr.decode("utf-8", errors="replace")
        notes = []
        if len(out) > MAX_OUTPUT_CHARS or len(err) > MAX_OUTPUT_CHARS:
            notes.append("output_truncated")
        return ProcessOutcome("ok" if p.returncode == 0 else "failed", p.returncode, out[:MAX_OUTPUT_CHARS], err[:MAX_OUTPUT_CHARS], dt, notes)
