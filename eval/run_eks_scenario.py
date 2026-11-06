"""EKS 1.35→1.36 업그레이드 시나리오를 실제 모델(Bedrock)과 mock 실행기로 한 번 끝까지 돌려 본다(SPEC 36).

접수(READ) → 계획 에이전트(MAIN) → 리뷰어(SUB), 읽기 도구는 루프백 moto 서버와 시뮬레이터에서 실행된다. 실제 AWS 리소스는 건드리지 않는다.
모델 호출 비용이 든다(한 번에 약 30~40만 토큰). 결과(소요 시간, 토큰, step 수, 위험 등급)만 출력하고 요청·계획 원문은 저장하지 않는다.
`--case N`이면 평가셋(`eval/agent_cases.csv`) N번 케이스의 입력으로 돌린다(단계별 호출 계측용, 채점은 하지 않음). `--mode loop|batch`로 계획 조사 방식을 고른다(기본은 PLANNER_MODE). 숫자만 `eval/reports/planner_mode_<날짜>.jsonl`에 한 줄씩 추가한다(방식 비교용).

    .venv\\Scripts\\python.exe eval\\run_eks_scenario.py
"""
from __future__ import annotations

import io
import json
import sys
from collections import Counter
import tempfile
import time
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
from app.planner.scenarios import load_scenarios  # noqa: E402
from app.rag.chunks import load_chunks  # noqa: E402
from app.rag.retriever import HybridRetriever  # noqa: E402

REQUEST = ("dev-main 클러스터를 1.35에서 1.36으로 업그레이드하는 계획을 세워줘. 노드는 ASG 로 관리하는 self-managed 이고 "
           "인프라는 Terraform 으로 관리해. eks-workers-a ASG 의 노드를 교체해야 해")


def main() -> None:
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else config.planner_mode()
    chunks = load_chunks()
    masker = Masker(Inventory.from_file(ROOT / "tests" / "fixtures" / "inventory.sample.yaml"))
    ws = Path(tempfile.mkdtemp())
    guard = CommandGuard(CommandIndex(chunks), CliSnapshot(), workspace=ws)
    ex = MockExecutor(guard, ws)
    try:
        ex.seed()
        budget = AgentBudget()                                    # 기본 한도(토큰 700,000, 600초)
        llm = LLMClient(masker, budget=budget)
        tb = ReadToolbox(masker=masker, guard=guard, retriever=HybridRetriever(chunks, vector_index=None), runner=ReadRunnerAdapter(ex, guard, masker),
                         budget=budget, search_kwargs={"method": "bm25"})
        pipe = PlanPipeline(Intake(llm, masker), PlanningAgent(llm, tb, guard, masker, budget, mode=mode, stage_prompts="--stage-prompts" in sys.argv, phase_docs="--no-phase-docs" not in sys.argv,
                                                             scenarios=None if "--no-scenarios" in sys.argv else load_scenarios()), Reviewer(llm, masker), {c.id: c for c in chunks})
        t = time.perf_counter()
        text = REQUEST
        if "--case" in sys.argv:
            from eval.agent_cases import load_cases
            cid = int(sys.argv[sys.argv.index("--case") + 1])
            text = next(c["input"] for c in load_cases() if c["id"] == cid)
        r = pipe.run(text, iac_managed=True)
        secs = time.perf_counter() - t
        print(f"방식={mode}  소요 {secs:.0f}s  status={r.status}  reasons={r.reasons}")
        row = None
        if r.plan_result is not None:
            pr = r.plan_result
            print(f"계획 단계: status={pr.status} repairs={pr.repairs} usage={pr.usage} 모델 호출 {len(pr.token_log)}회")
            row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "case": int(sys.argv[sys.argv.index("--case") + 1]) if "--case" in sys.argv else 9, "mode": mode, "seconds": round(secs), "status": r.status, "reasons": list(r.reasons), "usage": pr.usage,
                   "stage_prompts": "--stage-prompts" in sys.argv, "phase_docs": "--no-phase-docs" not in sys.argv and mode == "batch", "scenarios": "--no-scenarios" not in sys.argv and mode == "batch", "model_calls": len(pr.token_log),
                   "model_calls_by_stage": dict(Counter(pr.stage_log)), "tool_calls_by_stage": dict(Counter(f"{a}:{b}" for a, b in pr.tool_log)), "repairs": pr.repairs, "input_tokens": sum(a for a, _ in pr.token_log),
                   "output_tokens": sum(b for _, b in pr.token_log), "checkpoint": list(pr.checkpoint.progress) if pr.checkpoint else None}
        if r.status == "plan":
            plan = r.plan_result.plan
            steps = plan.all_steps()
            print(f"phase {len(plan.phases)}개 / step {len(steps)}개 (자동 삽입 {sum(s.auto for s in steps)}, 폴백 {sum(s.fallback for s in steps)}, "
                  f"변경 {sum(s.op == 'mutate' for s in steps)}, iac {sum(s.kind == 'iac' for s in steps)})")
            print("게이트:", [(p.id, p.gate, p.gate_locked) for p in plan.phases])
            print("경고:", plan.warnings)
            print("리뷰 위험 등급:", r.review.facts.risk_level, "| 요약:", bool(r.review.summary), r.review.reasons)
            row.update(phases=len(plan.phases), steps=len(steps))
            # phase별 구조(게이트 여부, 변경 step 수, 되돌릴 수 없음 표시): 방식·프롬프트에 따라 계획 구조가 달라지는지 숫자로 비교한다(내용 없음)
            row["phase_profile"] = [{"gate": p.gate, "mutate": sum(x.op == "mutate" for x in p.steps), "iac": sum(x.kind == "iac" for x in p.steps),
                                     "irreversible": sum(bool(getattr(x, "irreversible", False)) for x in p.steps), "n": len(p.steps)} for p in plan.phases]
            out = ROOT / "data" / "last_scenario_plan.json"                # 마스킹된 계획만 저장한다(실제 값 없음). 리뷰어 등을 오프라인에서 다시 시험하는 데 쓴다
            out.parent.mkdir(exist_ok=True)
            out.write_text(plan.model_dump_json(indent=1), encoding="utf-8")
        if row is not None:
            rep = ROOT / "eval" / "reports" / f"planner_mode_{time.strftime('%Y%m%d')}.jsonl"
            with rep.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    finally:
        ex.close()


if __name__ == "__main__":
    main()
