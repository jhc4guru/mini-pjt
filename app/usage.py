"""모델 호출 토큰 사용량의 날짜별 기록(D-53). Bedrock은 남은 일일 토큰 한도를 알려 주지 않고 이 키에는 Service Quotas·CloudWatch 조회 권한도 없어서,
앱이 직접 \"오늘 모델별로 얼마를 썼고 언제 쓰로틀링에 걸렸는지\"를 남긴다. 평가처럼 토큰을 많이 쓰는 작업을 나눠 돌릴 때 참고한다(한도 자체는 모른다).

- 기록하는 것은 날짜(UTC), 모델 ID, 역할, 호출 수, 입력·출력 토큰, 쓰로틀링 횟수와 마지막 시각뿐이다(요청·응답 내용 없음).
- 기록 실패는 모델 호출을 막지 않는다(`record*`는 예외를 삼킨다).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage(
  day TEXT NOT NULL, model_id TEXT NOT NULL, role TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  throttled INTEGER NOT NULL DEFAULT 0, last_throttle_ts REAL, last_ok_ts REAL,
  PRIMARY KEY(day, model_id, role)
)
"""


def utc_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).strftime("%Y-%m-%d")


class UsageLog:
    def __init__(self, path: str | Path, *, clock=time.time):
        self._clock, self._lock = clock, threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def record_call(self, model_id: str, role: str, input_tokens: int, output_tokens: int) -> None:
        now = self._clock()
        self._upsert(model_id, role, "calls=calls+1, input_tokens=input_tokens+?, output_tokens=output_tokens+?, last_ok_ts=?", (int(input_tokens), int(output_tokens), now), (1, int(input_tokens), int(output_tokens), 0, None, now))

    def record_throttle(self, model_id: str, role: str) -> None:
        now = self._clock()
        self._upsert(model_id, role, "throttled=throttled+1, last_throttle_ts=?", (now,), (0, 0, 0, 1, now, None))

    def _upsert(self, model_id: str, role: str, update_sql: str, update_args: tuple, insert_vals: tuple) -> None:
        try:
            with self._lock:
                day = utc_day(self._clock())
                cur = self._db.execute(f"UPDATE usage SET {update_sql} WHERE day=? AND model_id=? AND role=?", (*update_args, day, model_id, role))
                if cur.rowcount == 0:
                    self._db.execute("INSERT INTO usage(day,model_id,role,calls,input_tokens,output_tokens,throttled,last_throttle_ts,last_ok_ts) VALUES(?,?,?,?,?,?,?,?,?)", (day, model_id, role, *insert_vals))
                self._db.commit()
        except sqlite3.Error:
            pass                                                              # 기록 실패는 모델 호출을 막지 않는다

    def today(self, model_id: str | None = None) -> list[dict]:
        q, a = "SELECT day, model_id, role, calls, input_tokens, output_tokens, throttled, last_throttle_ts, last_ok_ts FROM usage WHERE day=?", [utc_day(self._clock())]
        if model_id:
            q, a = q + " AND model_id=?", a + [model_id]
        with self._lock:
            rows = self._db.execute(q + " ORDER BY model_id, role", a).fetchall()
        keys = ("day", "model_id", "role", "calls", "input_tokens", "output_tokens", "throttled", "last_throttle_ts", "last_ok_ts")
        return [dict(zip(keys, r)) for r in rows]

    def tokens_today(self, model_id: str) -> int:
        return sum(r["input_tokens"] + r["output_tokens"] for r in self.today(model_id))

    def total_tokens_today(self) -> int:
        return sum(r["input_tokens"] + r["output_tokens"] for r in self.today())
