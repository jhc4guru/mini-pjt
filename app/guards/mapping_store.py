"""마스킹 매핑(플레이스홀더 ↔ 실제 값)의 세션별 암호화 저장소. D-29.

서버 재시작 후에도 승인된 plan을 이어서 실행하려면 실제 값을 되살려야 한다. 실제 값은 AES-256-GCM으로 암호화해
SQLite에 두고, 키는 DB 밖 파일(`mapping.key`)에 둔다. 세션 id를 AAD로 묶어 다른 세션의 암호문으로 바꿔치기할 수 없다.
마지막 갱신 후 TTL이 지나면 삭제하고, 세션이 끝나면 `delete`로 지운다. 인터페이스(`MappingStore`)는 이후 Redis 등으로 교체할 수 있다.
오류 메시지에는 값을 담지 않는다. 복호화 실패는 "없음"이 아니라 예외다(fail-closed).
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import DATA_DIR
from app.guards.keyfile import key_path_for, load_or_create_key
from app.guards.masking import Masker

DEFAULT_TTL_SECONDS = 24 * 3600


class MappingUnreadable(Exception):
    """저장된 매핑을 복호화할 수 없다(키 불일치·변조). 값은 오류에 담지 않는다."""


class MappingStore(ABC):
    @abstractmethod
    def save(self, session_id: str, masker: Masker) -> None: ...

    @abstractmethod
    def load_into(self, session_id: str, masker: Masker) -> bool:
        """저장된 매핑을 masker에 되살린다. 없거나 만료면 False."""

    @abstractmethod
    def delete(self, session_id: str) -> None: ...

    @abstractmethod
    def purge_expired(self) -> int: ...


class SqliteMappingStore(MappingStore):
    def __init__(self, path: Path | str | None = None, *, ttl_seconds: float = DEFAULT_TTL_SECONDS, clock=time.time):
        self._path = str(path) if path is not None else str(DATA_DIR / "mapping.sqlite")
        if self._path == ":memory:":
            key = secrets.token_bytes(32)
        else:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            key = load_or_create_key(key_path_for(self._path, "mapping.key"))
        self._aes = AESGCM(key[:32])
        self._ttl, self._clock = ttl_seconds, clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS mapping_state ("
            "session_id TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, updated REAL NOT NULL)"
        )
        self._conn.commit()

    def save(self, session_id: str, masker: Masker) -> None:
        nonce = os.urandom(12)
        blob = json.dumps(masker.export_state(), ensure_ascii=False).encode("utf-8")
        ct = self._aes.encrypt(nonce, blob, session_id.encode("utf-8"))
        with self._lock:
            self._conn.execute(
                "INSERT INTO mapping_state (session_id, nonce, ciphertext, updated) VALUES (?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET nonce=excluded.nonce, ciphertext=excluded.ciphertext, updated=excluded.updated",
                (session_id, nonce, ct, self._clock()),
            )
            self._conn.commit()

    def load_into(self, session_id: str, masker: Masker) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT nonce, ciphertext, updated FROM mapping_state WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            return False
        nonce, ct, updated = row
        if self._clock() - updated > self._ttl:
            self.delete(session_id)
            return False
        try:
            state = json.loads(self._aes.decrypt(nonce, ct, session_id.encode("utf-8")))
            masker.load_state(state)
        except (InvalidTag, ValueError, KeyError, TypeError) as e:
            raise MappingUnreadable(f"세션 매핑을 읽을 수 없다({type(e).__name__})") from None
        return True

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM mapping_state WHERE session_id=?", (session_id,))
            self._conn.commit()

    def purge_expired(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM mapping_state WHERE ? - updated > ?", (self._clock(), self._ttl))
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        self._conn.close()
