"""moto 서버(AWS API 모사)를 별도 프로세스로 띄운다. 루프백 주소에만 바인딩하고, mock 외의 엔드포인트는 허용하지 않는다."""
from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from app.executor.process import isolated_env

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
DUMMY_CREDENTIALS = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "us-east-1"}


def assert_local_endpoint(url: str) -> None:
    """mock 모드는 실제 AWS로 나가지 않는다. 루프백이 아닌 엔드포인트는 거부한다."""
    host = urlparse(url).hostname or ""
    if host not in LOOPBACK or not url.startswith("http://"):
        raise ValueError("mock 엔드포인트는 루프백(http://127.0.0.1)만 허용한다")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MotoServer:
    def __init__(self, python: str | None = None):
        self._python = python or sys.executable
        self._proc: subprocess.Popen | None = None
        self._tmp = tempfile.TemporaryDirectory(prefix="moto-")
        self.port = 0

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 40.0) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self.port = _free_port()
        env = isolated_env(Path(self._tmp.name), {**DUMMY_CREDENTIALS})
        self._proc = subprocess.Popen([self._python, "-m", "moto.server", "-H", "127.0.0.1", "-p", str(self.port)], env=env,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError("moto 서버가 종료되었다(moto[server] 설치 확인)")
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                assert_local_endpoint(self.endpoint)
                return
            except OSError:
                time.sleep(0.3)
        self.close()
        raise RuntimeError("moto 서버가 제때 뜨지 않았다")

    def close(self) -> None:
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait(timeout=10)
            self._proc = None
        self._tmp.cleanup()
