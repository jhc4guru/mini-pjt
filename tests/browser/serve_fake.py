"""수동 브라우저 확인용(pytest에 포함되지 않음). `cdp.js`와 함께 쓴다. 실제 화면을 가짜 계획 파이프라인(첫 응답은 되묻기, 다음은 계획)과 스크립트 실행기에 연결해 띄운다. 모델·AWS를 쓰지 않는다."""
import os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import uvicorn
from app.guards.cli_snapshot import CliSnapshot
from app.rag.chunks import load_chunks
from app.web.server import create_app
from test_service import build

chunks = load_chunks()
snap = CliSnapshot()
snap.build([(c.cli, c.service, c.name) for c in chunks], only_missing=True)
box = build(Path(tempfile.mkdtemp(prefix="uifake-")), chunks, snap, statuses=["clarify", "plan", "plan", "plan"], sleep=lambda s: None)
uvicorn.run(create_app(box.svc), host="127.0.0.1", port=8124, log_level="warning")
