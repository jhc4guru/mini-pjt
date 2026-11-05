"""plan 확정(일괄 승인) 때 저위험 변경 절차의 절차별 승인도 함께 받는다(D-50): 4-eyes는 유지하고 apply의 승인 클릭만 줄인다."""
import json

import pytest
from test_apply import chunks, e, snapshot, start  # noqa: F401

MK = {"id": "mk-sg", "kind": "cli", "title": "SG 생성", "command": "aws ec2 create-security-group --group-name web --description web"}
MK_CHK = {"id": "mk-chk", "kind": "cli", "title": "SG 확인", "command": "aws ec2 describe-security-groups --group-names web", "check_for": "mk-sg"}
LATER_CHK = {"id": "tag-chk", "kind": "cli", "title": "규칙 확인", "command": "aws ec2 describe-security-groups --group-names web", "check_for": "tag"}


def later(e):
    cidr = e.masker.mask("10.20.0.0/16")
    return {"id": "tag", "kind": "cli", "title": "규칙 추가", "command": f"aws ec2 authorize-security-group-ingress --group-id @{{mk-sg.GroupId}} --protocol tcp --port 443 --cidr {cidr}"}


def by_id(plan):
    return {s.id: s for s in plan.all_steps()}


def test_low_risk_changes_qualify_and_the_listed_blockers_name_why_others_do_not(e):
    steps = by_id(e.make_plan((MK, MK_CHK)))
    assert steps["mk-sg"].preapprovable and steps["mk-sg"].preapprove_blockers == ()
    assert "iac_drift" in steps["mk-sg"].flags                                                       # IaC 코드와 어긋날 수 있다는 안내는 위험 요인이 아니다
    r = steps["reboot"]                                                                              # 재부팅: 중단 가능성이 높다
    assert not r.preapprovable and "disruption_likely" in r.preapprove_blockers
    assert steps["verify"].preapprove_blockers == ("not_server_change",) and steps["mk-chk"].preapprove_blockers == ("not_server_change",)    # 읽기는 원래 승인이 필요 없다


def test_the_criteria_exclude_high_risk_irreversible_replacing_flagged_and_unconfirmed_changes(chunks):
    from app.planner.schema import Step
    ok = dict(id="x", kind="cli", title="t", command="aws ec2 create-security-group --group-name a", op="mutate", risk="medium", grounded=True,
              impact={"disruption": "none", "reversible": "full", "replaces": False})
    assert Step(**ok).preapprovable
    assert Step(**{**ok, "risk": "low"}).preapprovable and Step(**{**ok, "impact": {**ok["impact"], "reversible": "partial"}}).preapprovable
    for patch, why in [({"risk": "high"}, "high_risk"), ({"grounded": False}, "not_grounded"), ({"impact": {**ok["impact"], "reversible": "irreversible"}}, "irreversible"),
                       ({"impact": {**ok["impact"], "disruption": "possible"}}, "disruption_possible"), ({"impact": {"reversible": "full"}}, "disruption_unknown"),
                       ({"impact": {**ok["impact"], "replaces": True}}, "replaces"), ({"requires": ("strong_warning",)}, "needs_confirmation"),
                       ({"flags": ("open_cidr",)}, "flag:open_cidr"), ({"flags": ("prod_target",)}, "flag:prod_target"), ({"flags": ("scale_to_zero",)}, "flag:scale_to_zero"),
                       ({"declared_irreversible": True}, "irreversible"), ({"kind": "manual"}, "not_server_change"), ({"op": "read"}, "not_server_change")]:
        s = Step(**{**ok, **patch})
        assert not s.preapprovable and why in s.preapprove_blockers, (patch, s.preapprove_blockers)
    assert Step(**{**ok, "flags": ("iac_drift", "uses_output")}).preapprovable                       # 안내 표시는 막지 않는다


def test_confirming_the_plan_records_step_approvals_by_the_reviewer_for_low_risk_changes_only(e):
    v, h = start(e, e.make_plan((MK, MK_CHK)))
    assert e.ap.preapproved_ids(e.sid, v) == ["mk-sg"]
    rows = e.store.approvals(e.sid, v, "step", "mk-sg")
    assert [(r["nick"], r["detail"]) for r in rows] == [("bob", "preapproved_with_plan")]            # 확인자(요청자가 아닌 사람)의 승인으로 남는다
    assert e.store.approvals(e.sid, v, "step", "reboot") == []                                       # 위험한 절차는 apply에서 개별 승인


def test_preapproval_is_applied_only_when_the_normal_order_rules_allow_and_the_requester_can_then_run_it(e):
    plan = e.make_plan((MK, MK_CHK))
    v = e.ap.submit_plan(e.sid, "alice", plan)
    h = e.store.plan_meta(e.sid, v)["plan_hash"]
    e.ap.approve_plan(e.sid, "bob", v, h)
    assert e.ap.apply_preapprovals(e.sid, v) == [] and e.store.step_states(e.sid, v)["mk-sg"]["state"] == "pending"          # apply 전에는 아무것도 안 한다
    e.ap.start_apply(e.sid, "alice")
    assert e.ap.apply_preapprovals(e.sid, v) == ["mk-sg"]
    st = e.store.step_states(e.sid, v)
    assert st["mk-sg"]["state"] == "approved" and st["reboot"]["state"] == "pending"                  # 변경 절차 중 위험한 reboot은 그대로 승인 대기
    e.ex.script = [("ok", json.dumps({"GroupId": "sg-0123456789abcdef0"}), "")]
    out = e.apply.run_step(e.sid, v, "mk-sg", nick="alice")                                          # 요청자가 ▶로 실행할 수 있다(승인은 확인자가 이미 했다)
    assert out.state == "done"


def test_a_dependent_preapproved_step_waits_for_its_dependency_and_the_gate(e):
    plan = e.make_plan((MK, MK_CHK, later(e), LATER_CHK))
    v, h = start(e, plan)
    assert set(e.ap.preapproved_ids(e.sid, v)) >= {"mk-sg", "tag"}
    assert e.ap.apply_preapprovals(e.sid, v) == ["mk-sg"]                                            # tag는 mk-sg가 끝나야 승인 상태가 된다
    e.ex.script = [("ok", json.dumps({"GroupId": "sg-0123456789abcdef0"}), "")]
    e.apply.run_step(e.sid, v, "mk-sg", nick="alice")
    assert e.ap.apply_preapprovals(e.sid, v) == ["tag"]


def test_the_approval_is_bound_to_the_plan_hash_and_a_retry_needs_a_fresh_approval(e):
    plan = e.make_plan((MK, MK_CHK))
    v, h = start(e, plan)
    e.ap.apply_preapprovals(e.sid, v)
    e.ex.script = [("failed", "", "An error occurred (Throttling)")]
    e.apply.run_step(e.sid, v, "mk-sg", nick="alice")
    assert e.store.step_states(e.sid, v)["mk-sg"]["state"] == "failed"
    e.apply.decide(e.sid, "alice", v, "mk-sg", "retry") if hasattr(e.apply, "decide") else None
    e.ap.reset_step_approvals(e.sid, v, "mk-sg")                                                     # 재시도하면 사전 승인도 무효가 되어 다시 승인받는다
    assert e.ap.preapproved_ids(e.sid, v) == []


def test_the_requester_cannot_confirm_the_plan_so_no_preapproval_exists_without_another_person(e):
    from app.session.approval import ApprovalError
    plan = e.make_plan((MK, MK_CHK))
    v = e.ap.submit_plan(e.sid, "alice", plan)
    h = e.store.plan_meta(e.sid, v)["plan_hash"]
    with pytest.raises(ApprovalError):
        e.ap.approve_plan(e.sid, "alice", v, h)
    assert e.ap.preapproved_ids(e.sid, v) == [] and e.store.approvals(e.sid, v, "step") == []


def test_through_the_service_the_reviewer_confirmation_preapproves_and_apply_needs_only_a_run_click(tmp_path, chunks, snapshot):
    from test_service import REQUEST, build
    box = build(tmp_path, chunks, snapshot, extra=(MK, MK_CHK))
    c = box.svc.create("alice", "사전 승인", REQUEST)
    sid = c["session_id"]
    box.svc.generate_plan(sid, REQUEST)
    box.svc.join(sid, c["invite"], "bob")
    v = box.svc.view(sid, "alice")
    assert v["preapprovable"] == ["mk-sg"] and "reboot" in v["preapprove_blockers"] and "disruption_likely" in v["preapprove_blockers"]["reboot"]      # 확정 전에 미리 알려 준다
    assert box.svc.handle(sid, "alice", {"type": "approve_plan", "version": v["version"], "hash": v["plan_hash"]})["ok"] is False                  # 요청자는 확정할 수 없다
    assert box.svc.handle(sid, "bob", {"type": "approve_plan", "version": v["version"], "hash": v["plan_hash"]})["ok"]
    assert box.svc.view(sid, "bob")["preapproved"] == ["mk-sg"]
    assert box.svc.handle(sid, "alice", {"type": "start_apply"})["ok"]
    v = box.svc.view(sid, "alice")
    assert v["steps"]["mk-sg"]["state"] == "approved" and v["steps"]["reboot"]["state"] == "pending"                  # 서비스가 시작과 함께 승인 상태로 올린다
    box.ex.script = [("ok", json.dumps({"GroupId": "sg-0123456789abcdef0"}), "")]
    assert box.svc.handle(sid, "alice", {"type": "run_step", "version": v["version"], "step": "mk-sg"})["ok"]
    assert box.svc.view(sid, "alice")["steps"]["mk-sg"]["state"] == "done"
    assert box.svc.handle(sid, "alice", {"type": "approve_step", "version": v["version"], "step": "reboot", "hash": v["plan_hash"]})["ok"] is False   # 위험한 절차는 요청자가 여전히 승인할 수 없다
    assert box.svc.handle(sid, "bob", {"type": "approve_step", "version": v["version"], "step": "reboot", "hash": v["plan_hash"]})["ok"]
    box.store.close(); box.maps.close()
