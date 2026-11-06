"""로컬 트레이스(T7-1, D-60): 모델 호출·읽기 도구 호출·계획 생성 결과의 **메타데이터만** SQLite에 남긴다.

- 자유 텍스트는 저장하지 않는다. 요청·프롬프트·모델 출력·도구 결과·명령 원문은 어디에도 기록하지 않고, 이름·원인 코드·토큰 수·지연·성공 여부만 남긴다.
- 그래도 안전을 한 번 더 확인한다(fail-closed): 문자열 필드는 안전한 문자 집합만 허용하고, 민감값·시크릿 형태(`patterns.find_sensitive`·`find_secrets`)가 보이면
  그 필드를 버리고 `blocked`로 표시한 뒤 종류 이름만 남긴다. 이 검사는 끌 수 없다.
- 백엔드는 `TRACE_BACKEND`(기본 `local`): `off`는 아무것도 기록하지 않는다. `langfuse`는 이 단계에서 구현하지 않았고(개발 단계 한정, 명시적으로 켜는 것만 허용하는 규칙이 있다) 요청하면 시작을 거부한다.
- 세션 id가 trace id다(세션 ↔ 계획 해시는 `pipeline` 이벤트로 연결한다).
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from app.guards.patterns import find_secrets, find_sensitive

KINDS = ("llm", "tool", "pipeline")
_SAFE = re.compile(r"[A-Za-z0-9_.:\-/,>]{0,100}")          # 이름·코드·모델 ID·세션 id·해시에 쓰이는 문자만
_STR_FIELDS = ("name", "role", "actual_role", "model", "code", "session_id", "plan_hash")
GUARD_BLOCK_CODES = frozenset({
    "not_executable_read_command", "command_not_masked", "query_not_masked", "unknown_placeholder",
    "references_not_allowed", "tool_input_contains_sensitive_value", "repeated_call", "bad_arguments",
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '', actual_role TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0, cache_write INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 1, code TEXT NOT NULL DEFAULT '', n INTEGER NOT NULL DEFAULT 0,
  plan_hash TEXT NOT NULL DEFAULT '', blocked TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS trace_session ON trace_events(session_id, id);
"""


class TraceConfigError(RuntimeError):
    """지원하지 않는 트레이스 백엔드를 요청했다(시작을 거부한다)."""


class NullTracer:
    """`TRACE_BACKEND=off`: 아무것도 기록하지 않는다."""

    def record(self, kind: str, name: str = "", **_) -> None:
        return None

    def events(self, *_, **__) -> list[dict]:
        return []

    def summary(self, *_, **__) -> dict:
        return {}

    def close(self) -> None:
        return None


def _clean(field: str, value, blocked: list[str]) -> str:
    s = "" if value is None else str(value)
    if not _SAFE.fullmatch(s):
        blocked.append(f"{field}:unsafe_chars")
        return "[blocked]"
    kinds = [k for k, _ in find_sensitive(s)] + list(find_secrets(s))
    if kinds:
        blocked.append(f"{field}:{','.join(sorted(set(kinds)))}")
        return "[blocked]"
    return s


class LocalTracer:
    def __init__(self, path: Path | str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    # ---- 기록 ----
    def record(self, kind: str, name: str = "", *, session_id: str | None = None, role: str = "", actual_role: str = "", model: str = "",
               input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0, cache_write: int = 0, latency_ms: int = 0,
               ok: bool = True, code: str = "", n: int = 0, plan_hash: str = "") -> None:
        """이벤트 하나. 오류가 나도 호출한 쪽(모델 호출·도구 호출)을 깨뜨리지 않는다."""
        try:
            if kind not in KINDS:
                kind = "tool"
            blocked: list[str] = []
            vals = {f: _clean(f, v, blocked) for f, v in zip(_STR_FIELDS, (name, role, actual_role, model, code, session_id, plan_hash))}
            if blocked:
                vals["code"] = "trace_leak_blocked" if vals["code"] in ("", "[blocked]") else vals["code"]
            row = (time.time(), vals["session_id"], kind, vals["name"], vals["role"], vals["actual_role"], vals["model"],
                   int(input_tokens), int(output_tokens), int(cache_read), int(cache_write), int(latency_ms), 1 if ok else 0,
                   vals["code"], int(n), vals["plan_hash"], ";".join(blocked)[:200])
            with self._lock:
                self._db.execute(
                    "INSERT INTO trace_events(ts,session_id,kind,name,role,actual_role,model,input_tokens,output_tokens,cache_read,cache_write,"
                    "latency_ms,ok,code,n,plan_hash,blocked) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
                self._db.commit()
        except Exception:  # noqa: BLE001  관측 실패가 본 작업을 막지 않는다
            return

    # ---- 조회 ----
    def events(self, session_id: str | None = None, limit: int = 200) -> list[dict]:
        q, args = "SELECT * FROM trace_events", []
        if session_id:
            q += " WHERE session_id=?"
            args.append(session_id)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            cur = self._db.execute(q, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def summary(self, since: float | None = None, session_id: str | None = None) -> dict:
        """개발용 지표: 호출·토큰, 폴백 비율, 쓰로틀링, 가드 차단 비율, 재시도(수정 시도), 계획 결과."""
        where, args = [], []
        if since is not None:
            where.append("ts>=?")
            args.append(since)
        if session_id:
            where.append("session_id=?")
            args.append(session_id)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT kind,name,role,actual_role,input_tokens,output_tokens,cache_read,cache_write,latency_ms,ok,code,n,blocked FROM trace_events{w}", args).fetchall()
        llm = [r for r in rows if r[0] == "llm"]
        tools = [r for r in rows if r[0] == "tool"]
        pipes = [r for r in rows if r[0] == "pipeline"]
        done = [r for r in llm if r[9]]
        by_role: dict[str, dict] = {}
        for r in done:
            d = by_role.setdefault(r[3] or r[2], {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            d["calls"] += 1
            d["input_tokens"] += r[4]
            d["output_tokens"] += r[5]
        fallbacks = sum(1 for r in done if r[2] and r[3] and r[2] != r[3])
        blocks = sum(1 for r in tools if r[10] in GUARD_BLOCK_CODES)
        outcomes: dict[str, int] = {}
        for r in pipes:
            outcomes[r[1]] = outcomes.get(r[1], 0) + 1
        return {
            "llm_calls": len(done), "llm_failures": len(llm) - len(done), "throttles": sum(1 for r in llm if not r[9] and r[10] == "throttled"),
            "input_tokens": sum(r[4] for r in done), "output_tokens": sum(r[5] for r in done),
            "cache_read_tokens": sum(r[6] for r in done), "cache_write_tokens": sum(r[7] for r in done),
            "avg_latency_ms": (sum(r[8] for r in done) // len(done)) if done else 0, "by_role": by_role,
            "fallback_calls": fallbacks, "fallback_ratio": (fallbacks / len(done)) if done else 0.0,
            "tool_calls": len(tools), "tool_errors": sum(1 for r in tools if not r[9]), "guard_blocks": blocks,
            "guard_block_ratio": (blocks / len(tools)) if tools else 0.0,
            "pipelines": outcomes, "repairs": sum(r[11] for r in pipes), "blocked_events": sum(1 for r in rows if r[12]),
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()


def make_tracer(path: Path | str, env: dict | None = None):
    """`TRACE_BACKEND`(기본 local)에 맞는 트레이서. langfuse 등 지원하지 않는 값은 시작을 거부한다."""
    backend = ((env if env is not None else os.environ).get("TRACE_BACKEND") or "local").strip().lower()
    if backend == "off":
        return NullTracer()
    if backend == "local":
        return LocalTracer(path)
    raise TraceConfigError(f"TRACE_BACKEND={backend}은 지원하지 않는다(local 또는 off). Langfuse 연동은 이 단계에서 구현하지 않았다.")
