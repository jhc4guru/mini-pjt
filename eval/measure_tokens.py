"""계획 생성의 호출당 토큰과 프롬프트 캐싱 효과를 실제 모델(Bedrock)과 mock 실행기로 한 번 측정한다(D-54).

접수(READ) → 계획 에이전트(MAIN) → 리뷰어(SUB). 읽기 도구는 루프백 moto 서버와 시뮬레이터에서 실행되며 실제 AWS 리소스는 건드리지 않는다.
모델 호출 비용이 든다(요청 하나에 약 5~10만 토큰). 한 번에 한 설정만 돌린다.

    .venv\\Scripts\\python.exe eval\\measure_tokens.py              # 프롬프트 캐싱 켬(운영 설정)
    .venv\\Scripts\\python.exe eval\\measure_tokens.py --no-cache   # 끔(기준선)
    .venv\\Scripts\\python.exe eval\\measure_tokens.py --request "..."

출력: 호출별 (입력, 출력, 캐시 읽기, 캐시 쓰기) 표와 합계. 요청·계획 원문은 저장하지 않고 숫자만 eval/reports/tokens_<시각>.json에 남긴다.
입력 환산은 어림값이다(Anthropic 표준 배율: 캐시 읽기 0.1배, 5분 캐시 쓰기 1.25배. 실제 요금표가 아니다).
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
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

from app import config  # noqa: E402

config.load_env()

from app.agents.tools import ReadToolbox  # noqa: E402
from app.executor.adapter import ReadRunnerAdapter  # noqa: E402
from app.executor.mock import MockExecutor  # noqa: E402
from app.guards.agent_limits import AgentBudget  # noqa: E402
from app.guards.cli_snapshot import CliSnapshot  # noqa: E402
from app.guards.command_guard import CommandGuard, CommandIndex  # noqa: E402
from app.guards.masking import Inventory, Masker  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.planner.agent import PlanningAgent  # noqa: E402
from app.planner.intake import Intake  # noqa: E402
from app.planner.pipeline import PlanPipeline  # noqa: E402
from app.planner.review import Reviewer  # noqa: E402
from app.rag.chunks import load_chunks  # noqa: E402
from app.rag.retriever import HybridRetriever  # noqa: E402

DEFAULT_REQUEST = "default vpc 에 제일 싼 서버 한대 띄우고 싶어"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cache", action="store_true", help="프롬프트 캐싱을 끄고 측정한다(기준선)")
    ap.add_argument("--request", default=DEFAULT_REQUEST)
    args = ap.parse_args()

    chunks = load_chunks()
    masker = Masker(Inventory.from_file(ROOT / "tests" / "fixtures" / "inventory.sample.yaml"))
    ws = Path(tempfile.mkdtemp())
    guard = CommandGuard(CommandIndex(chunks), CliSnapshot(), workspace=ws)
    ex = MockExecutor(guard, ws)
    try:
        ex.seed()
        budget = AgentBudget()
        llm = LLMClient(masker, budget=budget, prompt_cache=not args.no_cache)
        tb = ReadToolbox(masker=masker, guard=guard, retriever=HybridRetriever(chunks, vector_index=None), runner=ReadRunnerAdapter(ex, guard, masker),
                         budget=budget, search_kwargs={"method": "bm25"})
        pipe = PlanPipeline(Intake(llm, masker), PlanningAgent(llm, tb, guard, masker, budget), Reviewer(llm, masker), {c.id: c for c in chunks})
        t = time.perf_counter()
        r = pipe.run(args.request, drafts=True)
        elapsed = time.perf_counter() - t
    finally:
        ex.close()

    pr = r.plan_result
    print(f"설정: 프롬프트 캐싱 {'끔' if args.no_cache else '켬'} | region={config.region()} | 소요 {elapsed:.0f}s | status={r.status} reasons={r.reasons}")
    if pr is None:
        print("계획 단계에 도달하지 못했다(접수에서 끝남). 측정할 호출이 없다.")
        return
    rows = []
    print(f"{'#':>3} {'입력(전체)':>10} {'출력':>7} {'캐시읽기':>9} {'캐시쓰기':>9}")
    for i, ((inp, out), (cr, cw)) in enumerate(zip(pr.token_log, pr.cache_log), 1):
        rows.append({"call": i, "input": inp, "output": out, "cache_read": cr, "cache_write": cw})
        print(f"{i:>3} {inp:>10,} {out:>7,} {cr:>9,} {cw:>9,}")
    t_in = sum(x["input"] for x in rows)
    t_out = sum(x["output"] for x in rows)
    t_read = sum(x["cache_read"] for x in rows)
    t_write = sum(x["cache_write"] for x in rows)
    fresh = t_in - t_read - t_write
    equiv = fresh + 0.1 * t_read + 1.25 * t_write
    print(f"합계: 호출 {len(rows)}회 | 입력 {t_in:,} (캐시 읽기 {t_read:,} = {100 * t_read / t_in if t_in else 0:.0f}%, 쓰기 {t_write:,}) | 출력 {t_out:,}")
    print(f"입력 환산(어림, 캐시 읽기 0.1배·쓰기 1.25배): {equiv:,.0f}  (캐시 없을 때 {t_in:,}의 {100 * equiv / t_in if t_in else 0:.0f}%)")
    print(f"계획 상태: {pr.status} | 예산 사용: {pr.usage}")
    out = ROOT / "eval" / "reports" / f"tokens_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"cache": not args.no_cache, "region": config.region(), "elapsed_s": round(elapsed), "status": r.status,
                               "calls": rows, "total_input": t_in, "total_output": t_out, "cache_read": t_read, "cache_write": t_write,
                               "input_equiv": round(equiv)}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
