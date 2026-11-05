"""가드 판정 감사 기록(SQLite).

모든 가드 판정(통과/차단 사유)을 남긴다. 입력 텍스트는 저장하지 않는다: 차단된 입력에는 시크릿이 있을 수 있다.
대신 길이와 HMAC-SHA256(설치별 키, 앞 12자)만 남겨 같은 입력을 연결할 수 있게 한다. 시크릿 차단 입력은 해시도 남기지 않는다. 사유는 코드뿐이다.
이력 전체 스키마(plan, step 등)는 T7에서 이 테이블과 합친다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from app.config import DATA_DIR
from app.guards.keyfile import key_path_for, load_or_create_key

STAGES = frozenset({"input", "command", "tool_result", "limits", "execution", "outbound", "approval"})


class AuditLog:
    def __init__(self, path: Path | str | None = None):
        self._path = str(path) if path is not None else str(DATA_DIR / "audit.sqlite")
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._key = self._load_key()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS guard_decisions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session_id TEXT, stage TEXT NOT NULL,"
            "allowed INTEGER NOT NULL, reasons TEXT NOT NULL, subject_hash TEXT, subject_len INTEGER)"
        )
        self._conn.commit()

    def _load_key(self) -> bytes:
        """설치마다 무작위인 HMAC 키. DB 밖 파일에 둔다(DB만 유출돼도 해시로 입력을 추측할 수 없게)."""
        if self._path == ":memory:":
            return secrets.token_bytes(32)
        return load_or_create_key(key_path_for(self._path, "audit.key"))

    def record(self, session_id: str | None, stage: str, allowed: bool, reasons: tuple[str, ...] | list[str] = (), subject: str | None = None) -> None:
        if stage not in STAGES:
            raise ValueError(f"알 수 없는 stage: {stage}")
        # 시크릿이 들어 있는 입력은 해시도 남기지 않는다(길이만).
        no_hash = any(str(r).startswith("secret_in_input") for r in reasons)
        h = hmac.new(self._key, subject.encode("utf-8"), hashlib.sha256).hexdigest()[:12] if subject is not None and not no_hash else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO guard_decisions (ts, session_id, stage, allowed, reasons, subject_hash, subject_len) VALUES (?,?,?,?,?,?,?)",
                (time.time(), session_id, stage, int(allowed), json.dumps(list(reasons), ensure_ascii=False), h, len(subject) if subject is not None else None),
            )
            self._conn.commit()

    def query(self, session_id: str | None = None, stage: str | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT ts, session_id, stage, allowed, reasons, subject_hash, subject_len FROM guard_decisions", []
        conds = []
        if session_id is not None:
            conds.append("session_id = ?")
            args.append(session_id)
        if stage is not None:
            conds.append("stage = ?")
            args.append(stage)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [
            {"ts": r[0], "session_id": r[1], "stage": r[2], "allowed": bool(r[3]), "reasons": json.loads(r[4]), "subject_hash": r[5], "subject_len": r[6]}
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()
