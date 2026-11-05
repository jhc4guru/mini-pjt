"""평가 시작 전 모델 점검(D-53). 모델마다 5토큰짜리 호출을 한 번 해서 쓰로틀링 여부를 보고, 앱이 기록한 오늘의 토큰 사용량을 함께 보여 준다.

주의: 이 점검이 통과해도 큰 요청이 끝까지 간다는 보장은 없다. Bedrock의 일일 토큰 한도는 남은 양을 알려 주지 않고(이 키에는 Service Quotas·CloudWatch 조회 권한도 없다)
작은 호출은 한도 직전에도 통과하기 때문이다. 그래서 오늘 이미 많이 썼다면 경고한다(`WARN_TOKENS_TODAY`).

    .venv\Scripts\python.exe eval\preflight.py            # MAIN·SUB·READ 모두
    .venv\Scripts\python.exe eval\preflight.py MAIN READ
"""
from __future__ import annotations

import datetime
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WARN_TOKENS_TODAY = 1_500_000            # 오늘 이 값 이상 썼다면 큰 작업 전에 경고한다(한도 자체는 모른다. 경험적 기준)
ROLES = ("MAIN", "SUB", "READ")


def _short(e: Exception) -> str:
    return re.sub(r"\d{12}", "<ACCT>", f"{type(e).__name__}: {e}")[:140]


def check_models(client, model_ids: dict[str, str], roles=ROLES, usage=None) -> dict[str, dict]:
    """역할별 {"model_id", "ok", "error", "throttled", "tokens_today", "last_throttle"}. `client`는 `converse`를 가진 객체."""
    out: dict[str, dict] = {}
    for role in roles:
        mid = model_ids[role]
        rec = {"model_id": mid, "ok": False, "error": None, "throttled": False, "tokens_today": 0, "last_throttle": None}
        if usage is not None:
            rows = usage.today(mid)
            rec["tokens_today"] = sum(r["input_tokens"] + r["output_tokens"] for r in rows)
            ts = [r["last_throttle_ts"] for r in rows if r["last_throttle_ts"]]
            rec["last_throttle"] = datetime.datetime.fromtimestamp(max(ts)).strftime("%H:%M:%S") if ts else None
        try:
            client.converse(modelId=mid, messages=[{"role": "user", "content": [{"text": "hi"}]}], inferenceConfig={"maxTokens": 5})
            rec["ok"] = True
        except Exception as e:  # noqa: BLE001
            rec["error"] = _short(e)
            rec["throttled"] = "Throttling" in rec["error"] or "TooManyRequests" in rec["error"]
        out[role] = rec
    return out


def verdict(results: dict[str, dict], need: tuple[str, ...]) -> tuple[bool, list[str]]:
    """큰 작업을 시작해도 되는가와 이유."""
    problems = []
    for role in need:
        r = results.get(role)
        if r is None or not r["ok"]:
            problems.append(f"{role} 사용 불가" + (" (일일 토큰 한도)" if r and r["throttled"] else f" ({r['error']})" if r else ""))
        elif r["tokens_today"] >= WARN_TOKENS_TODAY:
            problems.append(f"{role} 오늘 이미 {r['tokens_today']:,} 토큰 사용 — 큰 작업 중간에 한도에 걸릴 수 있음")
    return (not problems), problems


def main(argv: list[str]) -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.path.insert(0, str(ROOT))
    from app import config
    config.load_env()
    import boto3
    from app.usage import UsageLog

    roles = tuple(a.upper() for a in argv) or ROLES
    ids = {r: config.model_id(r) for r in ROLES}
    usage = UsageLog(config.DATA_DIR / "usage.sqlite")
    res = check_models(boto3.client("bedrock-runtime", region_name=config.region()), ids, roles, usage)
    for role, r in res.items():
        state = "OK" if r["ok"] else "쓰로틀링" if r["throttled"] else "오류"
        print(f"{role:5} {state:8} 오늘 사용 {r['tokens_today']:>10,} 토큰  마지막 쓰로틀링 {r['last_throttle'] or '-':8}  {r['model_id'].split('anthropic.')[-1]}" + (f"  {r['error']}" if r["error"] else ""))
    ok, why = verdict(res, roles)
    print("→ 시작 가능" if ok else "→ 시작하지 않는 것을 권합니다: " + "; ".join(why))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
