"""승인 엔진: 4-eyes, 해시 잠금, 버전, 순서·의존·게이트, requires 조치, 재시도 시 승인 무효."""
import sqlite3
from pathlib import Path

import pytest

from app.guards.audit import AuditLog
from app.guards.masking import Inventory, Masker
from app.planner.schema import Phase, Plan, Step
from app.session.approval import ApprovalEngine, ApprovalError
from app.session.store import PlanTampered, SessionStore

FIXTURE = Path(__file__).parent / "fixtures" / "inventory.sample.yaml"


def S(id, op="read", kind="cli", **kw):
    cmd = {"read": "aws ec2 describe-vpcs", "mutate": "aws ec2 reboot-instances --instance-ids <INSTANCE_1>"}.get(op)
    return Step(id=id, kind=kind, title=f"step {id}", command=kw.pop("command", cmd), op=op, **kw)


def plan(*, gate=False, p1_extra=(), p2_extra=()):
    p1 = Phase(id="p1", title="준비", steps=(S("a"), S("b", "mutate", depends_on=("a",)), S("c", check_for="b"), *p1_extra))
    p2 = Phase(id="p2", title="본작업", gate=gate, steps=(S("d", "mutate"), S("e", check_for="d"), *p2_extra))
    return Plan(summary="테스트", phases=(p1, p2))


@pytest.fixture()
def env(tmp_path):
    store = SessionStore(tmp_path / "s.sqlite")
    masker = Masker(Inventory.from_file(FIXTURE))
    audit = AuditLog(tmp_path / "a.sqlite")
    eng = ApprovalEngine(store, lambda sid: masker, audit=audit)
    sid, invite, _ = store.create_session("alice", "요청")
    store.join(sid, invite, "bob")
    store.join(sid, invite, "carol")
    yield eng, store, sid, audit, tmp_path
    store.close()


def submit(env, p=None):
    eng, store, sid, *_ = env
    v = eng.submit_plan(sid, "alice", p or plan())
    return v, store.plan_meta(sid, v)["plan_hash"]


def approved(env, p=None):
    eng, store, sid, *_ = env
    v, h = submit(env, p)
    eng.approve_plan(sid, "bob", v, h)
    eng.start_apply(sid, "alice")
    return v, h


def run(store, sid, v, step_id):
    """실행기가 한 일을 흉내 낸다: 승인된 step을 끝낸다."""
    store.transition_step(sid, v, step_id, "running")
    store.transition_step(sid, v, step_id, "done")


def denied(code, fn, *a, **kw):
    with pytest.raises(ApprovalError) as e:
        fn(*a, **kw)
    assert e.value.code.startswith(code), e.value.code


# ---------------- plan 승인 ----------------
def test_requester_cannot_approve_the_plan_but_another_participant_can(env):
    eng, store, sid, *_ = env
    v, h = submit(env)
    denied("requester_cannot_approve_plan", eng.approve_plan, sid, "alice", v, h)
    eng.approve_plan(sid, "bob", v, h)
    assert store.session(sid)["state"] == "plan_approved" and store.plan_meta(sid, v)["status"] == "approved"


def test_plan_approval_needs_the_matching_hash_and_a_participant(env):
    eng, store, sid, *_ = env
    v, h = submit(env)
    denied("plan_hash_mismatch", eng.approve_plan, sid, "bob", v, "0" * 64)
    denied("not_a_participant", eng.approve_plan, sid, "mallory", v, h)
    assert store.session(sid)["state"] == "plan_review"


def test_editing_creates_a_new_version_and_the_old_hash_is_stale(env):
    eng, store, sid, *_ = env
    v1, h1 = submit(env)
    v2 = eng.submit_plan(sid, "alice", plan(gate=True), parent_version=v1)
    denied("stale_plan_version", eng.approve_plan, sid, "bob", v1, h1)
    eng.approve_plan(sid, "bob", v2, store.plan_meta(sid, v2)["plan_hash"])
    assert store.plan_meta(sid, v1)["status"] == "superseded"


def test_editing_after_approval_drops_the_lock_and_needs_reapproval(env):
    eng, store, sid, *_ = env
    v, h = submit(env)
    eng.approve_plan(sid, "bob", v, h)
    v2 = eng.submit_plan(sid, "alice", plan(gate=True), parent_version=v)
    assert store.session(sid)["state"] == "plan_review"
    denied("cannot_start_apply:plan_review", eng.start_apply, sid, "alice")
    assert v2 == v + 1


def test_plan_cannot_be_edited_while_applying(env):
    eng, store, sid, *_ = env
    approved(env)
    denied("plan_edit_not_allowed:applying", eng.submit_plan, sid, "alice", plan(gate=True))


def test_apply_cannot_start_before_approval(env):
    eng, store, sid, *_ = env
    submit(env)
    denied("cannot_start_apply", eng.start_apply, sid, "alice")


def test_a_plan_changed_in_the_database_cannot_be_approved(env):
    eng, store, sid, audit, tmp = env
    v, h = submit(env)
    db = sqlite3.connect(tmp / "s.sqlite")
    db.execute("UPDATE plan_versions SET plan_json = REPLACE(plan_json, 'reboot-instances', 'terminate-instances')")
    db.commit()
    db.close()
    with pytest.raises(PlanTampered):
        eng.approve_plan(sid, "bob", v, h)


# ---------------- step 승인 ----------------
def test_requester_cannot_approve_a_change_step_but_can_run_reads(env):
    eng, store, sid, *_ = env
    v, h = approved(env)
    g = eng.approve_step(sid, "alice", v, "a", h)                       # 읽기는 요청자가 직접
    assert g.approved_by_other is False and store.step_states(sid, v)["a"]["state"] == "approved"
    run(store, sid, v, "a")
    denied("requester_cannot_approve_change", eng.approve_step, sid, "alice", v, "b", h)
    g = eng.approve_step(sid, "bob", v, "b", h)
    assert g.approved_by_other is True and g.approvers == ("bob",) and g.plan_hash == h


def test_step_approval_needs_the_locked_hash(env):
    eng, store, sid, *_ = env
    v, h = approved(env)
    denied("plan_hash_mismatch", eng.approve_step, sid, "bob", v, "a", "f" * 64)


def test_dependencies_and_phase_order_are_enforced(env):
    eng, store, sid, *_ = env
    v, h = approved(env)
    denied("dependency_not_done:a", eng.approve_step, sid, "bob", v, "b", h)
    denied("previous_phase_incomplete", eng.approve_step, sid, "bob", v, "d", h)
    eng.approve_step(sid, "alice", v, "a", h)
    run(store, sid, v, "a")
    eng.approve_step(sid, "bob", v, "b", h)
    denied("dependency_not_done:b", eng.approve_step, sid, "alice", v, "c", h)       # 확인 step은 변경 step이 끝난 뒤에


def test_a_step_cannot_be_approved_twice(env):
    eng, store, sid, *_ = env
    v, h = approved(env)
    eng.approve_step(sid, "alice", v, "a", h)
    denied("step_not_pending", eng.approve_step, sid, "alice", v, "a", h)


def finish_phase_one(eng, store, sid, v, h):
    eng.approve_step(sid, "alice", v, "a", h); run(store, sid, v, "a")
    eng.approve_step(sid, "bob", v, "b", h); run(store, sid, v, "b")
    eng.approve_step(sid, "alice", v, "c", h); run(store, sid, v, "c")


def test_gate_needs_a_non_requester_and_the_previous_phase_done(env):
    eng, store, sid, *_ = env
    v, h = approved(env, plan(gate=True))
    denied("previous_phase_incomplete", eng.approve_gate, sid, "bob", v, "p2")
    finish_phase_one(eng, store, sid, v, h)
    denied("gate_not_approved", eng.approve_step, sid, "bob", v, "d", h)
    denied("requester_cannot_approve_gate", eng.approve_gate, sid, "alice", v, "p2")
    denied("phase_has_no_gate", eng.approve_gate, sid, "bob", v, "p1")
    eng.approve_gate(sid, "bob", v, "p2")
    assert eng.approve_step(sid, "carol", v, "d", h).approved_by_other


def test_skipped_steps_count_as_done_for_order_and_dependencies(env):
    eng, store, sid, *_ = env
    v, h = approved(env)
    eng.approve_step(sid, "alice", v, "a", h); run(store, sid, v, "a")
    eng.approve_step(sid, "bob", v, "b", h)
    store.transition_step(sid, v, "b", "running"); store.transition_step(sid, v, "b", "failed"); store.transition_step(sid, v, "b", "skipped")
    eng.approve_step(sid, "alice", v, "c", h)


# ---------------- requires ----------------
def risky(req, flags=()):
    return plan(p1_extra=(), p2_extra=()).model_copy(update={"phases": (
        Phase(id="p1", title="준비", steps=(S("a"), S("b", "mutate", depends_on=("a",), requires=req, flags=flags), S("c", check_for="b"))),)})


def test_open_cidr_needs_the_requesters_reason_and_another_persons_confirmation(env):
    eng, store, sid, *_ = env
    v, h = approved(env, risky(("open_cidr_confirm",), ("open_cidr",)))
    eng.approve_step(sid, "alice", v, "a", h); run(store, sid, v, "a")
    denied("missing_requirement:open_cidr_confirm", eng.approve_step, sid, "bob", v, "b", h)
    denied("reason_missing", eng.confirm_requirement, sid, "bob", v, "b", "open_cidr_confirm")
    denied("reason_by_requester_only", eng.provide_reason, sid, "bob", v, "b", "외부 점검 때문에 필요합니다")
    eng.provide_reason(sid, "alice", v, "b", "협력사 점검을 위해 임시로 개방합니다")
    denied("requester_cannot_confirm", eng.confirm_requirement, sid, "alice", v, "b", "open_cidr_confirm")
    denied("missing_requirement", eng.approve_step, sid, "bob", v, "b", h)
    eng.confirm_requirement(sid, "bob", v, "b", "open_cidr_confirm")
    g = eng.approve_step(sid, "bob", v, "b", h)
    assert "open_cidr_confirm" in g.confirmations
    assert any(a["detail"] for a in store.approvals(sid, v, "confirm", "b:open_cidr_reason"))        # 사유가 이력에 남는다


def test_reason_goes_through_the_input_guard_and_is_stored_masked(env):
    eng, store, sid, *_ = env
    v, h = approved(env, risky(("open_cidr_confirm",)))
    denied("reason_blocked", eng.provide_reason, sid, "alice", v, "b", "비밀번호는 Passw0rd!x 입니다")
    denied("reason_too_short", eng.provide_reason, sid, "alice", v, "b", "ok")
    eng.provide_reason(sid, "alice", v, "b", "i-0abc123def4567890 점검 때문")
    assert "i-0abc123def4567890" not in store.approvals(sid, v, "confirm", "b:open_cidr_reason")[0]["detail"]


@pytest.mark.parametrize("req", ["strong_warning", "prod_warning", "show_file_contents"])
def test_other_warnings_need_a_non_requesters_confirmation(env, req):
    eng, store, sid, *_ = env
    v, h = approved(env, risky((req,)))
    eng.approve_step(sid, "alice", v, "a", h); run(store, sid, v, "a")
    denied("missing_requirement:" + req, eng.approve_step, sid, "bob", v, "b", h)
    denied("requester_cannot_confirm", eng.confirm_requirement, sid, "alice", v, "b", req)
    eng.confirm_requirement(sid, "bob", v, "b", req)
    assert req in eng.approve_step(sid, "bob", v, "b", h).confirmations


def test_a_confirmation_for_a_requirement_the_step_does_not_have_is_refused(env):
    eng, store, sid, *_ = env
    v, h = approved(env)
    denied("requirement_not_on_step", eng.confirm_requirement, sid, "bob", v, "b", "strong_warning")


def drycase(done):
    sync = "aws s3 sync ./dir s3://<BUCKET_1> --delete"
    p = Plan(summary="t", phases=(Phase(id="p1", title="x", steps=(
        S("dry", "mutate", command=sync + " --dryrun"), S("dchk", check_for="dry"),
        S("real", "mutate", command=sync, requires=("dry_run_first",), depends_on=("dchk",)), S("rchk", check_for="real"))),))
    return p


def test_dry_run_first_cannot_be_replaced_by_a_confirmation(env):
    eng, store, sid, *_ = env
    v, h = approved(env, drycase(False))
    denied("requirement_not_confirmable", eng.confirm_requirement, sid, "bob", v, "real", "dry_run_first")
    eng.approve_step(sid, "bob", v, "dry", h); run(store, sid, v, "dry")
    eng.approve_step(sid, "alice", v, "dchk", h); run(store, sid, v, "dchk")
    assert eng.approve_step(sid, "bob", v, "real", h).step_id == "real"            # 같은 명령의 dry-run이 앞서 끝났다


def test_dry_run_of_a_different_command_does_not_count(env):
    eng, store, sid, *_ = env
    p = Plan(summary="t", phases=(Phase(id="p1", title="x", steps=(
        S("dry", "mutate", command="aws s3 sync ./other s3://<BUCKET_1> --delete --dryrun"), S("dchk", check_for="dry"),
        S("real", "mutate", command="aws s3 sync ./dir s3://<BUCKET_1> --delete", requires=("dry_run_first",), depends_on=("dchk",)),
        S("rchk", check_for="real"))),))
    v, h = approved(env, p)
    eng.approve_step(sid, "bob", v, "dry", h); run(store, sid, v, "dry")
    eng.approve_step(sid, "alice", v, "dchk", h); run(store, sid, v, "dchk")
    denied("missing_requirement:dry_run_first", eng.approve_step, sid, "bob", v, "real", h)


def test_fallback_steps_need_another_persons_approval_even_if_read(env):
    eng, store, sid, *_ = env
    odd = S("odd", "read", grounded=False, requires=("fallback_approval",), command="aws ec2 describe-vpc-endpoints")
    v, h = approved(env, Plan(summary="t", phases=(Phase(id="p1", title="x", steps=(odd,)),)))
    denied("requester_cannot_approve_change", eng.approve_step, sid, "alice", v, "odd", h)
    assert eng.approve_step(sid, "bob", v, "odd", h).approved_by_other


# ---------------- 실행 직전 재계산 ----------------
def test_grant_is_recomputed_from_records_and_retry_revokes_it(env):
    eng, store, sid, *_ = env
    v, h = approved(env)
    eng.approve_step(sid, "alice", v, "a", h); run(store, sid, v, "a")
    eng.approve_step(sid, "bob", v, "b", h)
    assert eng.grant_for(sid, v, "b").approved_by_other
    store.transition_step(sid, v, "b", "running"); store.transition_step(sid, v, "b", "failed"); store.transition_step(sid, v, "b", "pending")
    eng.reset_step_approvals(sid, v, "b")
    denied("step_not_approved", eng.grant_for, sid, v, "b")
    eng.approve_step(sid, "carol", v, "b", h)                                   # 재시도는 다시 승인을 받는다
    assert eng.grant_for(sid, v, "b").approvers == ("carol",)


def test_grant_refuses_approval_records_made_for_another_hash(env):
    eng, store, sid, *_ = env
    v, h = approved(env)
    store.add_approval(sid, v, "step", "b", "bob", "e" * 64)                    # 다른 해시로 남은 승인 기록(위조·옛 버전)
    denied("step_not_approved", eng.grant_for, sid, v, "b")


# ---------------- manual·감사 ----------------
def test_manual_steps_wait_for_a_report_and_are_never_granted_for_execution(env):
    eng, store, sid, *_ = env
    man = S("m", "manual", kind="manual", command="kubectl exec web-0 -- ls")
    v, h = approved(env, Plan(summary="t", phases=(Phase(id="p1", title="x", steps=(man,)),)))
    assert eng.approve_step(sid, "alice", v, "m", h) is None
    assert store.step_states(sid, v)["m"]["state"] == "awaiting_report"


def test_every_decision_is_audited_with_a_code(env):
    eng, store, sid, audit, _ = env
    v, h = submit(env)
    denied("requester_cannot_approve_plan", eng.approve_plan, sid, "alice", v, h)
    eng.approve_plan(sid, "bob", v, h)
    rows = audit.query(session_id=sid, stage="approval")
    assert {tuple(r["reasons"]) for r in rows} >= {("requester_cannot_approve_plan",), ("plan_approved",)}
    assert any(not r["allowed"] for r in rows) and any(r["allowed"] for r in rows)
