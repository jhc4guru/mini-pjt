"""에이전트 평가 실행기(D-53): `eval/agent_cases.csv`를 실제 서비스 경로(빈 plan 세션의 초안 요청)로 돌려 규칙 채점하고, 선택하면 Judge로 채점해 리포트를 남긴다.

- 실제 모델(Bedrock)과 mock 실행기를 쓴다. 토큰이 많이 든다(계획 1건 약 5만~30만). 시작 전에 모델을 점검하고(`preflight`), 쓰로틀링이면 멈춘다.
- 결과는 케이스마다 `eval/reports/agent_<run_id>.jsonl`에 바로 추가한다. 한도 때문에 중간에 멈춰도 `--run-id`로 이어서 돌린다(이미 끝난 케이스는 건너뜀).
- 리포트에는 마스킹된 명령만 저장한다(실제 값 없음). 오류(모델·한도)는 정확도 분모에서 뺀다.

    .venv\\Scripts\\python.exe eval\\run_agent_eval.py --cases 19,20,42            # 일부
    .venv\\Scripts\\python.exe eval\\run_agent_eval.py --judge                        # 전체 + Judge(SUB)
    .venv\\Scripts\\python.exe eval\\run_agent_eval.py --run-id 20261106_0900         # 이어서
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPORTS = ROOT / "eval" / "reports"
CASE_TIMEOUT = 600


def observe_case(svc, text: str, title: str = "평가", *, timeout: int = CASE_TIMEOUT, poll=time.sleep) -> tuple[dict, str]:
    """빈 plan 세션에 초안을 요청하고 결과를 관측값으로 만든다(마스킹된 값만). (observed, session_id)."""
    c = svc.create("evaluator", title)
    sid = c["session_id"]
    r = svc.handle(sid, "evaluator", {"type": "answer", "text": text})
    masker = svc.masker_for(sid)
    if not r.get("ok"):
        return {"input_rejected": True, "reasons": [r.get("code", "")], "asked": False, "steps": [], "messages": [r.get("message") or r.get("code") or ""]}, sid
    end = time.time() + timeout
    while time.time() < end and svc.view(sid, "evaluator")["pipeline"]["status"] == "generating":
        poll(3)
    v = svc.view(sid, "evaluator")
    steps = [{"id": s["id"], "kind": s["kind"], "op": s["op"], "command": masker.mask(s["command"] or ""), "flags": s.get("flags", []), "title": masker.mask(s.get("title", ""))}
             for p in v["scratch"]["phases"] for s in p["steps"]]
    return {"input_rejected": False, "reasons": v["pipeline"].get("reasons") or [], "asked": any(m.get("kind") == "clarify" for m in v["thread"]), "steps": steps,
            "messages": [masker.mask(m["text"]) for m in v["thread"] if m["role"] == "assistant"][-6:]}, sid


def load_rows(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []


def previous_run(run_id: str) -> list[dict]:
    files = sorted((p for p in REPORTS.glob("agent_*.jsonl") if p.stem != f"agent_{run_id}"), key=lambda p: p.stat().st_mtime)
    return load_rows(files[-1]) if files else []


def main(argv: list[str] | None = None) -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", help="쉼표로 구분한 케이스 id(기본: 전체)")
    ap.add_argument("--judge", action="store_true", help="Judge(SUB 모델)로 기대 항목을 채점")
    ap.add_argument("--run-id", help="이어서 돌릴 실행 id")
    ap.add_argument("--force", action="store_true", help="시작 전 점검이 실패해도 진행")
    args = ap.parse_args(argv)

    from app import config
    config.load_env()
    import boto3
    from app.usage import UsageLog
    from app.web.factory import build_service
    from eval import preflight
    from eval.agent_cases import load_cases
    from eval.agent_scoring import compare, final_pass, render_markdown, score_rules, summarize
    from eval.judge import Judge

    need = ("MAIN", "READ") + (("SUB",) if args.judge else ())
    usage = UsageLog(config.DATA_DIR / "usage.sqlite")
    pf = preflight.check_models(boto3.client("bedrock-runtime", region_name=config.region()), {r: config.model_id(r) for r in preflight.ROLES}, need, usage)
    ok, why = preflight.verdict(pf, need)
    for role, r in pf.items():
        print(f"{role:5} {'OK' if r['ok'] else '불가'}  오늘 사용 {r['tokens_today']:,} 토큰")
    if not ok and not args.force:
        print("시작하지 않습니다:", "; ".join(why), "(--force로 무시할 수 있습니다)")
        return 1

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    REPORTS.mkdir(exist_ok=True)
    jsonl = REPORTS / f"agent_{run_id}.jsonl"
    done = {r["id"] for r in load_rows(jsonl)}
    cases = load_cases()
    if args.cases:
        want = {int(x) for x in args.cases.split(",")}
        cases = [c for c in cases if c["id"] in want]
    todo = [c for c in cases if c["id"] not in done]
    print(f"실행 {run_id}: {len(todo)}개 실행(이미 끝난 {len(done)}개 건너뜀)")

    run = build_service(data_dir=Path(tempfile.mkdtemp(prefix="agenteval-")), usage_path=config.DATA_DIR / "usage.sqlite")
    stopped_early = False
    try:
        for c in todo:
            before = usage.total_tokens_today()
            t0 = time.time()
            observed, sid = observe_case(run.service, c["input"], f"평가 {c['id']}")
            rules = score_rules(c["id"], observed)
            judge = None
            if args.judge and not rules["error"]:
                try:
                    masker = run.service.masker_for(sid)
                    llm = run.service._editor_factory(masker)._llm                       # 같은 마스커·사용량 기록을 쓰는 LLMClient
                    judge = Judge(llm, masker).evaluate(c, {**observed, "outcome": rules["outcome"]}, session_id=sid)
                except Exception as e:  # noqa: BLE001 — Judge 실패는 규칙 채점을 막지 않는다
                    judge = {"verdict": None, "error": type(e).__name__}
            row = {"id": c["id"], "category": c["category"], "outcome": rules["outcome"], "rules": rules, "judge": judge, "final": final_pass(rules, judge),
                   "tokens": usage.total_tokens_today() - before, "seconds": round(time.time() - t0), "steps": observed["steps"], "messages": observed["messages"]}
            with jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            mark = {True: "PASS", False: "FAIL", None: "ERR "}[row["final"]]
            print(f"#{c['id']:>2} {c['category']:9} {mark} {rules['outcome']:8} {row['seconds']:>4}s {row['tokens']:>8,}토큰  {', '.join(rules['failures'])[:90]}")
            if rules["error"] and any("llm_unavailable" in f for f in rules["failures"]):
                print("모델 한도로 멈춥니다. 한도가 풀린 뒤 같은 --run-id로 이어서 돌리세요:", run_id)
                stopped_early = True
                break
    finally:
        run.close()
    rows = load_rows(jsonl)
    summary = summarize(rows)
    prev = previous_run(run_id)
    diff = compare(prev, rows) if prev else None
    md = REPORTS / f"agent_{run_id}.md"
    md.write_text(render_markdown(run_id, summary, rows, diff, {"input": sum(r["tokens"] for r in rows), "output": 0, "fallbacks": 0}), encoding="utf-8")
    acc = summary["accuracy"]
    print(f"정확도 {acc * 100:.1f}% ({summary['total']})" if acc is not None else "채점된 케이스 없음", "| 리포트:", md)
    return 2 if stopped_early else 0


if __name__ == "__main__":
    raise SystemExit(main())
