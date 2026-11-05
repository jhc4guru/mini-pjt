"""EKS 업그레이드 시나리오를 세션 서비스로 처음부터 끝까지 돌린다(SPEC 36, T6 종단 확인).

실제 모델(Bedrock)로 계획을 만들고, 두 참여자(alice=요청자, bob=승인자)가 서비스의 동작 메시지(`handle`)로 승인·실행·보고·확인을 한다.
실행기는 mock(루프백 moto + 시뮬레이터)이라 실제 AWS를 건드리지 않는다. 승인자 역할을 스크립트가 대신하므로 이것은 승인 흐름의 동작 확인이지
사람 검토의 대체가 아니다. 결과(단계 수, 상태, 소요 시간, 토큰)만 출력하고 요청·계획 원문은 저장하지 않는다. 모델 호출 비용이 든다(약 30만 토큰).

    .venv\\Scripts\\python.exe eval\\run_eks_session.py
"""
from __future__ import annotations

import io
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

from app import config  # noqa: E402

config.load_env()

from app.web.factory import build_service  # noqa: E402

REQUEST = ("dev-main 클러스터를 1.35에서 1.36으로 업그레이드하는 계획을 세워줘. 노드는 ASG 로 관리하는 self-managed 이고 "
           "인프라는 Terraform 으로 관리해. eks-workers-a ASG 의 노드를 교체해야 해")
REQ, APP = "alice", "bob"


def main() -> None:
    running = build_service(data_dir=Path(tempfile.mkdtemp(prefix="eks-session-")))
    svc, store = running.service, running.store
    try:
        t0 = time.perf_counter()
        c = svc.create(REQ, "EKS 1.36 업그레이드", REQUEST, iac_managed=True)
        sid = c["session_id"]
        svc.join(sid, c["invite"], APP)
        res = svc.generate_plan(sid, REQUEST)
        print(f"계획 생성 {time.perf_counter() - t0:.0f}s status={res.status} reasons={res.reasons}")
        if res.status != "plan":
            return
        plan_usage = res.plan_result.usage
        v = store.latest_version(sid)
        plan = store.load_plan(sid, v)
        steps = plan.all_steps()
        print(f"phase {len(plan.phases)} / step {len(steps)} (변경 {sum(s.op == 'mutate' for s in steps)}, manual {sum(s.op == 'manual' for s in steps)}, "
              f"iac {sum(s.kind == 'iac' for s in steps)}, poll {sum(s.kind == 'poll' for s in steps)}) 위험 {res.review.facts.risk_level} 토큰 {plan_usage}")
        h = svc.view(sid, REQ)["plan_hash"]
        ok = lambda r, what: print(f"  {what}: {'OK' if r['ok'] else r}") or r["ok"]  # noqa: E731
        assert ok(svc.handle(sid, REQ, {"type": "approve_plan", "version": v, "hash": h}), "요청자 plan 승인 시도(거부돼야 함)") is False
        assert ok(svc.handle(sid, APP, {"type": "approve_plan", "version": v, "hash": h}), "승인자 plan 승인")
        assert ok(svc.handle(sid, REQ, {"type": "start_apply"}), "apply 시작")

        t1 = time.perf_counter()
        stuck = None
        for phase in plan.phases:
            if phase.gate:
                r = svc.handle(sid, APP, {"type": "approve_gate", "version": v, "phase": phase.id})
                if not r["ok"]:
                    stuck = (phase.id, "gate", r)
                    break
            for s in phase.steps:
                st = store.step_states(sid, v)[s.id]["state"]
                if st in ("done", "skipped"):
                    continue
                who = APP if (s.needs_other_approval) else REQ
                if st == "pending":                                              # 확인 step 등은 서버가 이미 승인·시작했을 수 있다
                    for req in svc.ap.pending_requirements(sid, v, s.id):
                        if req == "open_cidr_confirm":
                            svc.handle(sid, REQ, {"type": "provide_reason", "version": v, "step": s.id, "text": "점검을 위해 필요한 임시 개방입니다"})
                        svc.handle(sid, APP, {"type": "confirm_requirement", "version": v, "step": s.id, "requirement": req})
                    r = svc.handle(sid, who, {"type": "approve_step", "version": v, "step": s.id, "hash": h})
                    if not r["ok"]:
                        stuck = (s.id, "approve", r)
                        break
                if s.op == "manual" or s.op == "none":
                    svc.handle(sid, REQ, {"type": "report_done", "version": v, "step": s.id, "checked": list(range(len(s.checklist))), "text": ""})
                    r = svc.handle(sid, APP, {"type": "confirm_report", "version": v, "step": s.id, "accept": True})
                elif s.kind == "poll":
                    for _ in range(3):
                        svc.apply.poll_once(sid, v, s.id, nick=REQ)
                    r = svc.handle(sid, APP, {"type": "poll_complete", "version": v, "step": s.id})
                else:
                    r = svc.handle(sid, who, {"type": "run_step", "version": v, "step": s.id})
                if not r["ok"] or store.step_states(sid, v)[s.id]["state"] not in ("done", "skipped"):
                    stuck = (s.id, store.step_states(sid, v)[s.id]["state"], r)
                    break
            if stuck:
                break
        states = {}
        for x in store.step_states(sid, v).values():
            states[x["state"]] = states.get(x["state"], 0) + 1
        print(f"apply {time.perf_counter() - t1:.0f}s 세션={store.session(sid)['state']} step 상태={states}")
        if stuck:
            print("멈춤:", stuck)
    finally:
        running.close()


if __name__ == "__main__":
    main()
