"""apply 진행 엔진: 실행 선점, 확인 step 자동 실행, 실패 시 멈춤, 재시도·건너뛰기·중단 규칙, 출력 정리, 검토 중 읽기."""
from pathlib import Path

import pytest

from app.executor.base import ExecutionResult, Executor
from app.guards.audit import AuditLog
from app.guards.cli_snapshot import CliSnapshot, CliUnavailable
from app.guards.command_guard import CommandGuard, CommandIndex
from app.guards.masking import Inventory, Masker
from app.planner.failure import FailureAnalysis
from app.planner.schema import PlanDraft, build_plan
from app.rag.chunks import load_chunks
from app.session.apply import ApplyEngine
from app.session.approval import ApprovalEngine, ApprovalError
from app.session.store import SessionStore, StoreError

FIXTURE = Path(__file__).parent / "fixtures" / "inventory.sample.yaml"
INSTANCE = "i-0abc123def4567890"


class ScriptedExecutor(Executor):
    """`_run`만 구현한다(관문 `execute`는 실제 그대로: 승인·가드 재검사를 통과해야 실행된다)."""
    name, mock = "scripted", True

    def __init__(self, guard):
        super().__init__(guard)
        self.ran, self.script = [], []

    def _run(self, request):
        self.ran.append(request)
        status, out, err = self.script.pop(0) if self.script else ("ok", "{}", "")
        return ExecutionResult(request_id=request.request_id, status=status, exit_code=0 if status == "ok" else 1, stdout=out, stderr=err,
                               mock=True, executor="scripted")


@pytest.fixture(scope="module")
def chunks():
    return load_chunks()


@pytest.fixture(scope="module")
def snapshot(chunks):
    snap = CliSnapshot()
    try:
        snap.build([(c.cli, c.service, c.name) for c in chunks], only_missing=True)
    except CliUnavailable:
        pytest.skip("aws/kubectl이 필요합니다")
    return snap


@pytest.fixture()
def e(chunks, snapshot, tmp_path):
    masker = Masker(Inventory.from_file(FIXTURE))
    guard = CommandGuard(CommandIndex(chunks), snapshot, workspace=tmp_path)
    store = SessionStore(tmp_path / "s.sqlite")
    audit = AuditLog(tmp_path / "a.sqlite")
    ap = ApprovalEngine(store, lambda sid: masker, audit=audit)
    ex = ScriptedExecutor(guard)
    calls = []
    analyzer_box = {"fn": None}

    def analyzer(*a):
        calls.append(a)
        if analyzer_box["fn"] is None:
            raise RuntimeError("no analyzer")
        return analyzer_box["fn"](*a)

    ap_engine = ApplyEngine(store, ap, ex, guard, lambda sid: masker, analyzer=analyzer, audit=audit)
    sid, invite, _ = store.create_session("alice", "요청")
    store.join(sid, invite, "bob")
    store.join(sid, invite, "carol")
    ph = masker.mask(INSTANCE)

    def make_plan(extra_steps=(), gate=False):
        steps = [
            {"id": "reboot", "kind": "cli", "title": "재부팅", "command": f"aws ec2 reboot-instances --instance-ids {ph}"},
            {"id": "verify", "kind": "cli", "title": "확인", "command": f"aws ec2 describe-instance-status --instance-ids {ph}", "check_for": "reboot"},
            *extra_steps,
        ]
        d = PlanDraft.model_validate({"summary": "t", "phases": [{"id": "p1", "title": "phase", "steps": steps, "gate": gate}]})
        return build_plan(d, guard, masker, postprocess=False)

    class E:
        pass

    e = E()
    e.store, e.ap, e.apply, e.ex, e.audit, e.sid, e.masker, e.guard, e.make_plan, e.calls, e.analyzer = (
        store, ap, ap_engine, ex, audit, sid, masker, guard, make_plan, calls, analyzer_box)
    yield e
    store.close()


def start(e, plan=None):
    v = e.ap.submit_plan(e.sid, "alice", plan or e.make_plan())
    h = e.store.plan_meta(e.sid, v)["plan_hash"]
    e.ap.approve_plan(e.sid, "bob", v, h)
    e.ap.start_apply(e.sid, "alice")
    return v, h


def denied(code, fn, *a, **kw):
    with pytest.raises(ApprovalError) as ex:
        fn(*a, **kw)
    assert ex.value.code.startswith(code), ex.value.code


# ---------------- 정상 흐름 ----------------
def test_changed_step_runs_then_its_check_runs_automatically_and_the_session_completes(e):
    v, h = start(e)
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    out = e.apply.run_step(e.sid, v, "reboot", nick="bob")
    assert out.state == "done" and [c.step_id for c in out.checks] == ["verify"] and out.checks[0].state == "done"
    assert [r.step_id for r in e.ex.ran] == ["reboot", "verify"]
    assert e.store.session(e.sid)["state"] == "completed"


def test_executor_gets_the_real_value_but_stored_output_is_masked_and_secret_free(e):
    v, h = start(e)
    e.ex.script = [("ok", f"rebooted {INSTANCE} password=hunter2 AKIAIOSFODNN7EXAMPLE", "")]
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    e.apply.run_step(e.sid, v, "reboot", nick="bob")
    assert INSTANCE in e.ex.ran[0].command and INSTANCE not in e.ex.ran[0].masked_command
    text = e.store.outputs(e.sid, v, "reboot")[0]["text"]
    assert INSTANCE not in text and "hunter2" not in text and "AKIAIOSFODNN7EXAMPLE" not in text and "<INSTANCE_1>" in text


def test_unapproved_or_already_started_steps_do_not_run(e):
    v, h = start(e, e.make_plan(({"id": "peek", "kind": "cli", "title": "조회", "command": "aws ec2 describe-vpcs"},)))      # 끝나지 않는 step을 남겨 세션이 계속 apply 중이게 한다
    denied("step_not_approved", e.apply.run_step, e.sid, v, "reboot", nick="bob")
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    e.apply.run_step(e.sid, v, "reboot", nick="bob")
    with pytest.raises(StoreError, match="unexpected_state"):
        e.apply.run_step(e.sid, v, "reboot", nick="bob")                         # 이미 시작·완료된 step은 다시 실행되지 않는다
    assert [r.step_id for r in e.ex.ran].count("reboot") == 1


def test_running_needs_an_applying_session_and_a_participant(e):
    v = e.ap.submit_plan(e.sid, "alice", e.make_plan())
    denied("not_applying", e.apply.run_step, e.sid, v, "reboot", nick="bob")
    v, h = start(e)
    denied("not_a_participant", e.apply.run_step, e.sid, v, "reboot", nick="mallory")


def test_the_executor_never_receives_a_step_that_was_not_approved_by_another_person(e):
    v, h = start(e)
    denied("requester_cannot_approve_change", e.ap.approve_step, e.sid, "alice", v, "reboot", h)
    assert e.ex.ran == []


# ---------------- 실패 처리 ----------------
def test_failure_pauses_the_session_and_blocks_other_approvals(e):
    v, h = start(e)
    e.ex.script = [("failed", "", "An error occurred (ThrottlingException)")]
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    out = e.apply.run_step(e.sid, v, "reboot", nick="bob")
    assert out.state == "failed" and out.hint == "throttled" and e.store.session(e.sid)["state"] == "paused"
    denied("not_applying", e.ap.approve_step, e.sid, "bob", v, "verify", h)


def test_retry_after_a_transient_error_needs_a_fresh_approval(e):
    v, h = start(e)
    e.ex.script = [("failed", "", "ThrottlingException")]
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    e.apply.run_step(e.sid, v, "reboot", nick="bob")
    assert e.apply.decide(e.sid, "alice", v, "reboot", "retry") == "retry"
    denied("step_not_approved", e.apply.run_step, e.sid, v, "reboot", nick="bob")        # 이전 승인은 무효
    e.ap.approve_step(e.sid, "carol", v, "reboot", h)
    out = e.apply.run_step(e.sid, v, "reboot", nick="carol")
    assert out.state == "done" and e.store.step_states(e.sid, v)["reboot"]["attempts"] == 2 and e.store.session(e.sid)["state"] == "completed"


@pytest.mark.parametrize("err,code", [("AccessDenied", "permission"), ("InvalidParameterValue", "invalid_argument"), ("DependencyViolation", "dependency")])
def test_errors_that_will_repeat_cannot_be_retried(e, err, code):
    v, h = start(e)
    e.ex.script = [("failed", "", f"An error occurred ({err})")]
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    e.apply.run_step(e.sid, v, "reboot", nick="bob")
    denied(f"retry_not_allowed:{code}", e.apply.decide, e.sid, "alice", v, "reboot", "retry")


def test_retry_limit_is_enforced(e):
    v, h = start(e)
    for i in range(3):
        e.ex.script = [("failed", "", "ThrottlingException")]
        e.ap.approve_step(e.sid, "bob", v, "reboot", h)
        e.apply.run_step(e.sid, v, "reboot", nick="bob")
        if i < 2:
            e.apply.decide(e.sid, "alice", v, "reboot", "retry")
    denied("retry_limit_reached", e.apply.decide, e.sid, "alice", v, "reboot", "retry")


def test_a_timed_out_change_needs_a_state_check_before_retry(e):
    v, h = start(e)
    e.ex.script = [("timeout", "", "")]
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    out = e.apply.run_step(e.sid, v, "reboot", nick="bob")
    assert out.status == "timeout" and out.hint == "timeout"
    denied("state_check_required", e.apply.decide, e.sid, "alice", v, "reboot", "retry")
    assert e.apply.decide(e.sid, "alice", v, "reboot", "retry", state_checked=True) == "retry"


def test_skip_needs_another_person_and_no_dependents(e):
    extra = ({"id": "after", "kind": "cli", "title": "뒤", "command": "aws ec2 describe-vpcs", "depends_on": ["reboot"]},)
    v, h = start(e, e.make_plan(extra))
    e.ex.script = [("failed", "", "ThrottlingException")]
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    e.apply.run_step(e.sid, v, "reboot", nick="bob")
    denied("requester_cannot_skip", e.apply.decide, e.sid, "alice", v, "reboot", "skip")
    denied("skip_has_dependents:after", e.apply.decide, e.sid, "bob", v, "reboot", "skip")
    assert e.store.step_states(e.sid, v)["reboot"]["state"] == "failed"


def test_skipping_a_leaf_step_resumes_and_can_complete(e):
    v, h = start(e)
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    e.apply.run_step(e.sid, v, "reboot", nick="bob")                            # reboot, verify 모두 끝남 → completed
    assert e.store.session(e.sid)["state"] == "completed"


def test_abort_marks_open_steps_aborted_and_closes_the_session(e):
    v, h = start(e)
    e.ex.script = [("failed", "", "AccessDenied")]
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    e.apply.run_step(e.sid, v, "reboot", nick="bob")
    assert e.apply.decide(e.sid, "alice", v, "reboot", "abort") == "aborted"
    assert e.store.session(e.sid)["state"] == "aborted"
    assert {s["state"] for s in e.store.step_states(e.sid, v).values()} == {"aborted"}
    denied("not_paused", e.apply.decide, e.sid, "alice", v, "reboot", "retry")


def test_failed_check_pauses_and_retrying_the_check_runs_it_again_without_a_human_approval(e):
    v, h = start(e)
    e.ex.script = [("ok", "{}", ""), ("failed", "", "ThrottlingException")]
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    out = e.apply.run_step(e.sid, v, "reboot", nick="bob")
    assert out.state == "done" and out.checks[0].state == "failed" and e.store.session(e.sid)["state"] == "paused"
    e.apply.decide(e.sid, "alice", v, "verify", "retry")
    assert e.store.step_states(e.sid, v)["verify"]["state"] == "approved"
    o2 = e.apply.run_step(e.sid, v, "verify", nick="alice")
    assert o2.state == "done" and e.store.session(e.sid)["state"] == "completed"
    assert [r.step_id for r in e.ex.ran] == ["reboot", "verify", "verify"]


def test_decisions_need_a_participant_and_a_known_choice(e):
    v, h = start(e)
    e.ex.script = [("failed", "", "ThrottlingException")]
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    e.apply.run_step(e.sid, v, "reboot", nick="bob")
    denied("not_a_participant", e.apply.decide, e.sid, "mallory", v, "reboot", "retry")
    denied("unknown_choice", e.apply.decide, e.sid, "alice", v, "reboot", "force")


# ---------------- 실패 분석 연결 ----------------
def test_failure_analysis_is_recorded_masked_and_its_errors_do_not_block(e):
    v, h = start(e)
    e.ex.script = [("failed", "", "ThrottlingException")]
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    out = e.apply.run_step(e.sid, v, "reboot", nick="bob")                       # 분석기가 예외를 내도 진행은 멈추지 않는다
    assert out.state == "failed" and out.analysis is None and e.calls[0][1] == "reboot"
    e.apply.decide(e.sid, "alice", v, "reboot", "retry")
    e.analyzer["fn"] = lambda plan, step_id, result, attempt, sid: FailureAnalysis(
        "analysis", step_id=step_id, attempt=attempt, server_hint="throttled", cause=f"{INSTANCE} 호출 제한", category="throttled", recommendation="retry")
    e.ex.script = [("failed", "", "ThrottlingException")]
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    out = e.apply.run_step(e.sid, v, "reboot", nick="bob")
    assert out.analysis["recommendation"] == "retry" and INSTANCE not in out.analysis["cause"]
    logged = [x["detail"] for x in e.store.events(e.sid) if x["kind"] == "failure_analysis"]
    assert logged and INSTANCE not in logged[0]


# ---------------- 검토 중 읽기 ----------------
def test_read_steps_can_be_previewed_during_review_but_changes_cannot(e):
    plan = e.make_plan(({"id": "peek", "kind": "cli", "title": "조회", "command": "aws ec2 describe-vpcs"},))
    v = e.ap.submit_plan(e.sid, "alice", plan)
    e.ex.script = [("ok", '{"Vpcs": []}', "")]
    out = e.apply.preview_read(e.sid, v, "peek", nick="alice")
    assert out.status == "ok" and "Vpcs" in out.output and e.store.step_states(e.sid, v)["peek"]["state"] == "pending"
    denied("preview_read_only", e.apply.preview_read, e.sid, v, "reboot", nick="alice")
    denied("not_a_participant", e.apply.preview_read, e.sid, v, "peek", nick="mallory")
    assert [r.step_id for r in e.ex.ran] == ["peek"]


def test_preview_is_refused_for_ai_guess_reads_and_outside_review(e):
    plan = e.make_plan(({"id": "odd", "kind": "cli", "title": "추정", "command": "aws ec2 describe-vpc-endpoints"},
                        {"id": "oddchk", "kind": "cli", "title": "확인", "command": "aws ec2 describe-vpcs", "check_for": "odd"}))
    v = e.ap.submit_plan(e.sid, "alice", plan)
    denied("preview_read_only", e.apply.preview_read, e.sid, v, "odd", nick="alice")        # AI 추정 명령은 애매하면 변경으로 분류되어 실행하지 않는다
    h = e.store.plan_meta(e.sid, v)["plan_hash"]
    e.ap.approve_plan(e.sid, "bob", v, h)
    e.ap.start_apply(e.sid, "alice")
    denied("preview_not_allowed", e.apply.preview_read, e.sid, v, "odd", nick="alice")


def test_every_apply_decision_is_audited(e):
    v, h = start(e)
    e.ex.script = [("failed", "", "AccessDenied")]
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    e.apply.run_step(e.sid, v, "reboot", nick="bob")
    denied("retry_not_allowed", e.apply.decide, e.sid, "alice", v, "reboot", "retry")
    reasons = {r["reasons"][0] for r in e.audit.query(session_id=e.sid, stage="execution")}
    assert "step_failed:failed:permission" in reasons and "retry_not_allowed:permission" in reasons
