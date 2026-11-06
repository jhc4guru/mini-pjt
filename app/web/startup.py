"""서버 시작 전 점검(D-54). 가드나 승인 규칙이 아니라 **환경 점검**이다: 필수 CLI가 없으면 서버가 떠도 모든 명령이 `cli_unavailable`로 막혀 원인이 안 보이므로,
시작을 거부하고 이유와 설치 방법을 알려 준다. 루프백이 아닌 바인딩은 명시적으로 허용했을 때만 연다(컨테이너 안에서는 `ALLOW_NON_LOOPBACK=1`).
"""
from __future__ import annotations

import ipaddress
import os
import shutil
from typing import Callable

from app.guards.cli_snapshot import CliUnavailable, default_runner   # 프로세스는 이 통로로만 띄운다(실행기·스냅샷 모듈 외 spawn 금지 테스트)

INSTALL_HINT = {
    "aws": "AWS CLI v2: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html (Windows: winget install Amazon.AWSCLI)",
    "kubectl": "kubectl: https://kubernetes.io/docs/tasks/tools/ (Windows: winget install Kubernetes.kubectl)",
}


def _version(cmd: list[str], run: Callable[[list[str]], tuple[int, str]] | None = None) -> str | None:
    try:
        code, text = (run or default_runner)(cmd)
    except (OSError, CliUnavailable):
        return None
    out = text.strip().splitlines()
    return out[0] if code == 0 and out else None


def check_cli_tools(which: Callable = shutil.which, run: Callable | None = None) -> list[str]:
    """필수 CLI의 문제 목록(비었으면 통과). aws는 v2여야 하고 kubectl은 `version --client`가 동작해야 한다."""
    problems: list[str] = []
    if which("aws") is None:
        problems.append(f"`aws` CLI를 찾을 수 없습니다. 설치: {INSTALL_HINT['aws']}")
    else:
        v = _version(["aws", "--version"], run)
        if v is None or not v.startswith("aws-cli/2."):
            problems.append(f"AWS CLI v2가 필요합니다(지금: {v or '실행 실패'}). 설치: {INSTALL_HINT['aws']}")
    if which("kubectl") is None:
        problems.append(f"`kubectl`을 찾을 수 없습니다. 설치: {INSTALL_HINT['kubectl']}")
    elif _version(["kubectl", "version", "--client"], run) is None:
        problems.append(f"`kubectl version --client`가 실패합니다. 설치 확인: {INSTALL_HINT['kubectl']}")
    return problems


def is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind(host: str, env: dict | None = None) -> str | None:
    """루프백이 아닌 바인딩을 환경변수 `ALLOW_NON_LOOPBACK=1` 없이 요청하면 이유를 돌려준다."""
    e = os.environ if env is None else env
    if is_loopback(host) or e.get("ALLOW_NON_LOOPBACK") == "1":
        return None
    return (f"`--host {host}`는 루프백이 아닙니다. 이 앱의 인증은 닉네임·멤버 토큰뿐이라 다른 호스트에 열려면 ALLOW_NON_LOOPBACK=1을 명시해야 합니다"
            "(컨테이너에서는 호스트 쪽 포트를 -p 127.0.0.1:8000:8000처럼 127.0.0.1로만 여세요).")
