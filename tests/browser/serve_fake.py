"""수동 브라우저 확인용(pytest에 포함되지 않음). `cdp.js`와 함께 쓴다. 실제 화면을 가짜 계획 파이프라인(첫 응답은 되묻기, 다음은 계획)과 스크립트 실행기에 연결해 띄운다. 모델·AWS를 쓰지 않는다."""
import os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import uvicorn
from app.guards.cli_snapshot import CliSnapshot
from app.rag.chunks import load_chunks
from app.rag.retriever import HybridRetriever
from app.web.server import create_app
from test_service import build

chunks = load_chunks()
snap = CliSnapshot()
snap.build([(c.cli, c.service, c.name) for c in chunks], only_missing=True)
box = build(Path(tempfile.mkdtemp(prefix="uifake-")), chunks, snap, statuses=["clarify", "plan", "plan", "plan"], sleep=lambda s: None,
            catalog_retriever=HybridRetriever(chunks, vector_index=None))
# 말로 수정 확인용: 모델 대신 고정 수정안을 돌려주는 가짜 편집기(첫 절차 제목 바꾸기 + 마지막 절차 지우기가 아니라 제목만)
import json
from types import SimpleNamespace
from app.planner.scratch_edit import ScratchEditor


class _FixedLLM:
    def complete(self, role, user, **kw):
        out = {"summary": "첫 절차의 제목을 바꿉니다", "ops": [{"op": "update_step", "step_id": "step-1", "fields": {"title": "수정된 제목"}}]}
        return SimpleNamespace(text=json.dumps(out, ensure_ascii=False), requested_role=role, actual_role=role)


box.svc._editor_factory = lambda masker: ScratchEditor(_FixedLLM(), masker)
uvicorn.run(create_app(box.svc), host="127.0.0.1", port=8124, log_level="warning")
