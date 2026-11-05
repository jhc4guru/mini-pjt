"""서버 실행: `.venv\\Scripts\\python.exe -m app.web.main` (기본 127.0.0.1:8000, mock 실행기).

루프백에만 바인딩한다. 다른 호스트에 열려면 `--host`로 명시해야 하고, 그때는 인증이 닉네임·멤버 토큰뿐이라는 점(Phase 1)을 감안해야 한다.
"""
from __future__ import annotations

import argparse

import uvicorn

from app.web.factory import build_service
from app.web.server import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="DevOps Plan/Apply 에이전트 (Phase 1, mock 실행기)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    running = build_service()
    try:
        uvicorn.run(create_app(running.service), host=args.host, port=args.port, log_level="info")
    finally:
        running.close()


if __name__ == "__main__":
    main()
