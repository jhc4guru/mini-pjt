"""세션 저장소(SQLite). 세션, 참여자, plan 버전, step 상태, 승인, 이벤트를 저장한다.

- 저장하는 텍스트는 모두 마스킹된 형태다(실제 값과의 매핑은 `mapping_store`가 따로 암호화해 보관).
- plan은 JSON으로 저장하고 읽을 때 `plan_hash`를 다시 계산해 저장된 해시와 다르면 `PlanTampered`(승인 후 바뀐 계획은 쓰지 않는다).
- 상태 전이는 `states`의 표로만 가능하다. 승인과 이벤트는 추가만 가능하다(수정 API 없음, 승인 무효 처리는 이벤트로 남는다).
- 참여자는 세션마다 멤버 토큰을 받는다(해시로만 저장). 닉네임만으로 다른 사람 행세를 할 수 없다.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from app.planner.schema import Plan, plan_hash
from app.session import states

NICK = re.compile(r"^[A-Za-z0-9가-힣_-]{1,20}$")
MAX_DETAIL = 500
MAX_OUTPUT = 20000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, requester TEXT NOT NULL, state TEXT NOT NULL, masked_request TEXT NOT NULL,
  iac_managed INTEGER NOT NULL DEFAULT 0, invite_hash TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS participants(
  session_id TEXT NOT NULL, nick TEXT NOT NULL COLLATE NOCASE, role TEXT NOT NULL, token_hash TEXT NOT NULL, joined_at REAL NOT NULL,
  PRIMARY KEY(session_id, nick));
CREATE TABLE IF NOT EXISTS plan_versions(
  session_id TEXT NOT NULL, version INTEGER NOT NULL, plan_json TEXT NOT NULL, plan_hash TEXT NOT NULL, status TEXT NOT NULL,
  parent_version INTEGER, created_at REAL NOT NULL, PRIMARY KEY(session_id, version));
CREATE TABLE IF NOT EXISTS step_states(
  session_id TEXT NOT NULL, version INTEGER NOT NULL, step_id TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL, PRIMARY KEY(session_id, version, step_id));
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, version INTEGER NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
  nick TEXT NOT NULL COLLATE NOCASE, plan_hash TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', ts REAL NOT NULL,
  UNIQUE(session_id, version, kind, target, nick));
CREATE TABLE IF NOT EXISTS step_outputs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, version INTEGER NOT NULL, step_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  status TEXT NOT NULL, exit_code INTEGER, hint TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS session_meta(
  session_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(session_id, key));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, ts REAL NOT NULL, nick TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
"""


class StoreError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PlanTampered(StoreError):
    def __init__(self):
        super().__init__("plan_tampered")


def _h(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _Tx:
    def __init__(self, store: "SessionStore"):
        self._s = store

    def __enter__(self):
        self._s._lock.acquire()
        try:
            self._s._db.execute("BEGIN IMMEDIATE")
        except Exception:
            self._s._lock.release()
            raise
        return self._s._db

    def __exit__(self, et, ev, tb):
        try:
            self._s._db.execute("ROLLBACK" if et else "COMMIT")
        finally:
            self._s._lock.release()
        return False


class SessionStore:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time):
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._clock = clock
        with self._lock:
            self._db.executescript(SCHEMA)
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(sessions)")}
            if "title" not in cols:                                    # 제목 열이 없던 기존 DB를 이어서 쓴다
                self._db.execute("ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT ''")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _tx(self) -> _Tx:
        return _Tx(self)

    # ---------------- 세션·참여자 ----------------
    def create_session(self, requester: str, masked_request: str, *, iac_managed: bool = False, title: str = "") -> tuple[str, str, str]:
        """(세션 id, 초대 토큰, 요청자 멤버 토큰)을 돌려준다. 토큰은 해시만 저장한다."""
        nick = self._nick(requester)
        sid, invite, member = uuid.uuid4().hex, secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        now = self._clock()
        with self._tx() as db:
            db.execute("INSERT INTO sessions(id,requester,state,masked_request,iac_managed,invite_hash,created_at,updated_at,title) VALUES(?,?,?,?,?,?,?,?,?)",
                       (sid, nick, "draft", masked_request[:2000], int(iac_managed), _h(invite), now, now, title[:120]))
            db.execute("INSERT INTO participants VALUES(?,?,?,?,?)", (sid, nick, "requester", _h(member), now))
            self._event(db, sid, nick, "session_created", "")
        return sid, invite, member

    def join(self, session_id: str, invite_token: str, nick: str) -> str:
        nick = self._nick(nick)
        s = self.session(session_id)
        if s["state"] in ("completed", "aborted"):
            raise StoreError("session_closed")
        if not hmac.compare_digest(s["invite_hash"], _h(invite_token)):
            raise StoreError("invalid_invite")
        member = secrets.token_urlsafe(24)
        with self._tx() as db:
            try:
                db.execute("INSERT INTO participants VALUES(?,?,?,?,?)", (session_id, nick, "approver", _h(member), self._clock()))
            except sqlite3.IntegrityError:
                raise StoreError("nickname_taken") from None
            self._event(db, session_id, nick, "joined", "")
        return member

    def rotate_invite(self, session_id: str, *, nick: str | None = None) -> str:
        """새 초대 토큰을 만들어 이전 링크를 무효로 한다. 토큰은 해시만 저장하므로 만든 순간에만 볼 수 있다."""
        token = secrets.token_urlsafe(24)
        with self._tx() as db:
            db.execute("UPDATE sessions SET invite_hash=?, updated_at=? WHERE id=?", (_h(token), self._clock(), session_id))
            self._event(db, session_id, nick, "invite_created", "")
        return token

    def authenticate(self, session_id: str, member_token: str) -> str | None:
        """멤버 토큰에 해당하는 닉네임. 없거나 틀리면 None."""
        with self._lock:
            rows = self._db.execute("SELECT nick, token_hash FROM participants WHERE session_id=?", (session_id,)).fetchall()
        found = None
        for r in rows:                                         # 모두 비교해 시간 차이로 알아내지 못하게 한다
            if hmac.compare_digest(r["token_hash"], _h(member_token)):
                found = r["nick"]
        return found

    def session(self, session_id: str) -> dict:
        with self._lock:
            r = self._db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if r is None:
            raise StoreError("unknown_session")
        return dict(r)

    def participants(self, session_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT nick, role, joined_at FROM participants WHERE session_id=? ORDER BY joined_at", (session_id,))]

    def role_of(self, session_id: str, nick: str) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT role FROM participants WHERE session_id=? AND nick=?", (session_id, nick)).fetchone()
        return r["role"] if r else None

    def requester_of(self, session_id: str) -> str:
        return self.session(session_id)["requester"]

    def set_session_state(self, session_id: str, new: str, *, nick: str | None = None) -> None:
        with self._tx() as db:
            r = db.execute("SELECT state FROM sessions WHERE id=?", (session_id,)).fetchone()
            if r is None:
                raise StoreError("unknown_session")
            states.check_session(r["state"], new)
            db.execute("UPDATE sessions SET state=?, updated_at=? WHERE id=?", (new, self._clock(), session_id))
            self._event(db, session_id, nick, "session_state", f"{r['state']}->{new}")

    # ---------------- plan 버전 ----------------
    def add_plan_version(self, session_id: str, plan: Plan, *, parent_version: int | None = None, nick: str | None = None) -> int:
        with self._tx() as db:
            cur = db.execute("SELECT COALESCE(MAX(version),0) AS v FROM plan_versions WHERE session_id=?", (session_id,)).fetchone()["v"]
            v = cur + 1
            db.execute("UPDATE plan_versions SET status='superseded' WHERE session_id=? AND status IN ('draft','approved')", (session_id,))
            db.execute("INSERT INTO plan_versions VALUES(?,?,?,?,?,?,?)",
                       (session_id, v, plan.model_dump_json(), plan_hash(plan), "draft", parent_version, self._clock()))
            self._event(db, session_id, nick, "plan_version", f"v{v}")
        return v

    def latest_version(self, session_id: str) -> int | None:
        with self._lock:
            r = self._db.execute("SELECT MAX(version) AS v FROM plan_versions WHERE session_id=?", (session_id,)).fetchone()
        return r["v"]

    def plan_meta(self, session_id: str, version: int) -> dict:
        with self._lock:
            r = self._db.execute("SELECT version, plan_hash, status, parent_version, created_at FROM plan_versions WHERE session_id=? AND version=?",
                                 (session_id, version)).fetchone()
        if r is None:
            raise StoreError("unknown_plan_version")
        return dict(r)

    def load_plan(self, session_id: str, version: int) -> Plan:
        """저장된 plan을 읽고 해시를 다시 계산해 검증한다. 다르면 PlanTampered."""
        with self._lock:
            r = self._db.execute("SELECT plan_json, plan_hash FROM plan_versions WHERE session_id=? AND version=?", (session_id, version)).fetchone()
        if r is None:
            raise StoreError("unknown_plan_version")
        plan = Plan.model_validate_json(r["plan_json"])
        if not hmac.compare_digest(plan_hash(plan), r["plan_hash"]):
            raise PlanTampered()
        return plan

    def set_plan_status(self, session_id: str, version: int, status: str, *, nick: str | None = None) -> None:
        if status not in ("draft", "approved", "superseded"):
            raise StoreError("invalid_plan_status")
        with self._tx() as db:
            db.execute("UPDATE plan_versions SET status=? WHERE session_id=? AND version=?", (status, session_id, version))
            self._event(db, session_id, nick, "plan_status", f"v{version}:{status}")

    # ---------------- step 상태 ----------------
    def init_steps(self, session_id: str, version: int, step_ids: list[str], *, carry: dict[str, str] | None = None) -> None:
        """버전의 모든 step을 만든다. `carry`는 이전 버전에서 완료·건너뜀으로 이어받는 step의 상태다."""
        now, carry = self._clock(), carry or {}
        with self._tx() as db:
            for step_id in step_ids:
                st = carry.get(step_id, "pending")
                if st not in ("pending", "done", "skipped"):
                    raise StoreError("invalid_carry_state")
                db.execute("INSERT INTO step_states VALUES(?,?,?,?,?,?,?)",
                           (session_id, version, step_id, st, 0, "carried" if step_id in carry else "", now))

    def step_states(self, session_id: str, version: int) -> dict[str, dict]:
        with self._lock:
            rows = self._db.execute("SELECT step_id, state, attempts, detail FROM step_states WHERE session_id=? AND version=?", (session_id, version)).fetchall()
        return {r["step_id"]: {"state": r["state"], "attempts": r["attempts"], "detail": r["detail"]} for r in rows}

    def transition_step(self, session_id: str, version: int, step_id: str, new: str, *, expect: str | None = None,
                        detail: str = "", nick: str | None = None, count_attempt: bool = False) -> None:
        """`expect`가 있으면 현재 상태가 같을 때만 바꾼다(동시 요청에서 같은 step을 두 번 실행하지 않게 한다)."""
        with self._tx() as db:
            r = db.execute("SELECT state, attempts FROM step_states WHERE session_id=? AND version=? AND step_id=?", (session_id, version, step_id)).fetchone()
            if r is None:
                raise StoreError("unknown_step")
            if expect is not None and r["state"] != expect:
                raise StoreError(f"unexpected_state:{r['state']}")
            states.check_step(r["state"], new)
            db.execute("UPDATE step_states SET state=?, attempts=?, detail=?, updated_at=? WHERE session_id=? AND version=? AND step_id=?",
                       (new, r["attempts"] + (1 if count_attempt else 0), detail[:MAX_DETAIL], self._clock(), session_id, version, step_id))
            self._event(db, session_id, nick, "step_state", f"{step_id}:{r['state']}->{new}")

    # ---------------- 승인(추가만) ----------------
    def add_approval(self, session_id: str, version: int, kind: str, target: str, nick: str, phash: str, detail: str = "") -> bool:
        """승인을 추가한다. 같은 사람이 같은 대상에 다시 승인하면 False(중복 무시)."""
        if kind not in states.APPROVAL_KINDS:
            raise StoreError("invalid_approval_kind")
        with self._tx() as db:
            try:
                db.execute("INSERT INTO approvals(session_id,version,kind,target,nick,plan_hash,detail,ts) VALUES(?,?,?,?,?,?,?,?)",
                           (session_id, version, kind, target, nick, phash, detail[:MAX_DETAIL], self._clock()))
            except sqlite3.IntegrityError:
                return False
            self._event(db, session_id, nick, f"approval:{kind}", target)
        return True

    def approvals(self, session_id: str, version: int, kind: str | None = None, target: str | None = None) -> list[dict]:
        q, a = "SELECT kind, target, nick, plan_hash, detail, ts FROM approvals WHERE session_id=? AND version=?", [session_id, version]
        if kind:
            q, a = q + " AND kind=?", a + [kind]
        if target is not None:
            q, a = q + " AND target=?", a + [target]
        with self._lock:
            return [dict(r) for r in self._db.execute(q + " ORDER BY id", a)]

    def revoke_approvals(self, session_id: str, version: int, kind: str, target: str, *, nick: str | None = None) -> None:
        """재시도 등으로 다시 승인을 받아야 할 때 해당 대상의 승인을 무효 처리한다(기록은 events에 남는다)."""
        with self._tx() as db:
            db.execute("DELETE FROM approvals WHERE session_id=? AND version=? AND kind=? AND target=?", (session_id, version, kind, target))
            self._event(db, session_id, nick, f"approval_revoked:{kind}", target)

    # ---------------- 실행 결과(마스킹·정리된 텍스트만, 추가만) ----------------
    def add_output(self, session_id: str, version: int, step_id: str, attempt: int, status: str, exit_code: int | None, text: str, *, hint: str = "") -> None:
        with self._tx() as db:
            db.execute("INSERT INTO step_outputs(session_id,version,step_id,attempt,status,exit_code,hint,text,ts) VALUES(?,?,?,?,?,?,?,?,?)",
                       (session_id, version, step_id, attempt, status, exit_code, hint, text[:MAX_OUTPUT], self._clock()))

    def outputs(self, session_id: str, version: int, step_id: str | None = None) -> list[dict]:
        q, a = "SELECT step_id, attempt, status, exit_code, hint, text, ts FROM step_outputs WHERE session_id=? AND version=?", [session_id, version]
        if step_id is not None:
            q, a = q + " AND step_id=?", a + [step_id]
        with self._lock:
            return [dict(r) for r in self._db.execute(q + " ORDER BY id", a)]

    # ---------------- 세션 부가 정보(리뷰·근거 등 마스킹된 JSON) ----------------
    def set_meta(self, session_id: str, key: str, value: str) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO session_meta VALUES(?,?,?) ON CONFLICT(session_id,key) DO UPDATE SET value=excluded.value", (session_id, key, value))

    def get_meta(self, session_id: str, key: str) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT value FROM session_meta WHERE session_id=? AND key=?", (session_id, key)).fetchone()
        return r["value"] if r else None

    def sessions_in(self, *state: str) -> list[str]:
        with self._lock:
            q = ",".join("?" * len(state))
            return [r["id"] for r in self._db.execute(f"SELECT id FROM sessions WHERE state IN ({q})", state)]

    # ---------------- 이벤트(추가만) ----------------
    def log(self, session_id: str, kind: str, detail: str = "", *, nick: str | None = None) -> None:
        with self._tx() as db:
            self._event(db, session_id, nick, kind, detail)

    def events(self, session_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT ts, nick, kind, detail FROM events WHERE session_id=? ORDER BY id", (session_id,))]

    def _event(self, db, session_id: str, nick: str | None, kind: str, detail: str) -> None:
        db.execute("INSERT INTO events(session_id,ts,nick,kind,detail) VALUES(?,?,?,?,?)", (session_id, self._clock(), nick, kind, detail[:MAX_DETAIL]))

    @staticmethod
    def _nick(nick: str) -> str:
        if not isinstance(nick, str) or not NICK.match(nick):
            raise StoreError("invalid_nickname")
        return nick
