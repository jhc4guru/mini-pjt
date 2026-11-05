"""apply 진행: manual·iac 보고/확인, poll, 서버 재시작 복구. 픽스처는 `test_apply.py`와 같다."""
from test_apply import INSTANCE, chunks, denied, e, snapshot, start  # noqa: F401  (픽스처 재사용)

from app.session.apply import ApplyEngine

MANUAL = ({"id": "exec", "kind": "manual", "title": "직접 실행", "command": "kubectl exec web-0 -- ls"},
          {"id": "after-exec", "kind": "cli", "title": "확인", "command": "aws ec2 describe-vpcs", "check_for": "exec"})
POLL = ({"id": "wait", "kind": "poll", "title": "상태 대기", "command": "aws ec2 describe-instance-status --instance-ids <INSTANCE_1>",
         "poll": {"interval_seconds": 5, "max_seconds": 60, "until": "상태가 ok"}},)


# ---------------- manual·iac: 사람이 실행하고 보고, 다른 사람이 확인 ----------------
def test_the_server_never_runs_a_manual_step(e):
    v, h = start(e, e.make_plan(MANUAL))
    assert e.ap.approve_step(e.sid, "alice", v, "exec", h) is None
    denied("step_not_server_executable", e.apply.run_step, e.sid, v, "exec", nick="alice")
    assert all(r.step_id != "exec" for r in e.ex.ran)


def test_manual_step_is_reported_by_the_requester_and_confirmed_by_someone_else(e):
    v, h = start(e, e.make_plan(MANUAL))
    e.ap.approve_step(e.sid, "alice", v, "exec", h)
    denied("report_by_requester_only", e.apply.report_done, e.sid, "bob", v, "exec")
    denied("text_blocked", e.apply.report_done, e.sid, "alice", v, "exec", text="password=hunter2 로 접속했다")
    e.apply.report_done(e.sid, "alice", v, "exec", text=f"{INSTANCE} 에서 실행 완료")
    assert e.store.step_states(e.sid, v)["exec"]["state"] == "awaiting_confirm"
    assert INSTANCE not in e.store.outputs(e.sid, v, "exec")[0]["text"]
    denied("requester_cannot_confirm_report", e.apply.confirm_report, e.sid, "alice", v, "exec")
    e.apply.confirm_report(e.sid, "bob", v, "exec")
    assert e.store.step_states(e.sid, v)["exec"]["state"] == "done"
    assert [r.step_id for r in e.ex.ran] == ["after-exec"]                          # 확인 step이 자동 실행된다
    kinds = [a["kind"] for a in e.store.approvals(e.sid, v)]
    assert "report" in kinds and "verify" in kinds                                  # 보고자·확인자가 이력에 남는다


def test_rejected_report_waits_for_a_new_report(e):
    v, h = start(e, e.make_plan(MANUAL))
    e.ap.approve_step(e.sid, "alice", v, "exec", h)
    e.apply.report_done(e.sid, "alice", v, "exec")
    e.apply.confirm_report(e.sid, "bob", v, "exec", accept=False, note="결과가 다르다")
    assert e.store.step_states(e.sid, v)["exec"]["state"] == "awaiting_report"
    e.apply.report_done(e.sid, "alice", v, "exec")
    e.apply.confirm_report(e.sid, "carol", v, "exec")
    assert e.store.step_states(e.sid, v)["exec"]["state"] == "done"


def test_a_failed_manual_report_pauses_and_goes_through_the_same_choices(e):
    v, h = start(e, e.make_plan(MANUAL))
    e.ap.approve_step(e.sid, "alice", v, "exec", h)
    e.apply.report_failed(e.sid, "alice", v, "exec", text="권한이 없어 실패")
    assert e.store.session(e.sid)["state"] == "paused"
    assert e.apply.decide(e.sid, "alice", v, "exec", "retry") == "retry"
    assert e.store.step_states(e.sid, v)["exec"]["state"] == "pending"


def test_iac_step_needs_every_checklist_item_before_the_report(e):
    iac = ({"id": "tf", "kind": "iac", "title": "코드 반영", "checklist": ["PR 생성", "plan 결과 확인"]},)
    v, h = start(e, e.make_plan(iac))
    e.ap.approve_step(e.sid, "alice", v, "tf", h)
    denied("checklist_incomplete", e.apply.report_done, e.sid, "alice", v, "tf", checked=[0])
    e.apply.report_done(e.sid, "alice", v, "tf", checked=[0, 1], text="terraform plan: No changes")
    e.apply.confirm_report(e.sid, "bob", v, "tf")
    assert e.store.step_states(e.sid, v)["tf"]["state"] == "done"


def test_reports_are_refused_for_steps_the_server_runs(e):
    v, h = start(e)
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    denied("not_a_reported_step", e.apply.report_done, e.sid, "alice", v, "reboot")


# ---------------- poll ----------------
def test_poll_keeps_running_until_a_person_confirms_the_condition(e):
    v, h = start(e, e.make_plan(POLL))
    e.ap.approve_step(e.sid, "alice", v, "wait", h)
    denied("poll_step_uses_poll_api", e.apply.run_step, e.sid, v, "wait", nick="alice")
    o = e.apply.poll_once(e.sid, v, "wait", nick="alice")
    assert o.state == "running" and e.store.step_states(e.sid, v)["wait"]["state"] == "running"
    e.apply.poll_once(e.sid, v, "wait", nick="alice")
    assert [x["status"] for x in e.store.outputs(e.sid, v, "wait")] == ["poll:1", "poll:2"]
    e.apply.poll_complete(e.sid, "bob", v, "wait")
    assert e.store.step_states(e.sid, v)["wait"]["state"] == "done"


def test_poll_cannot_be_confirmed_before_it_started(e):
    v, h = start(e, e.make_plan(POLL))
    e.ap.approve_step(e.sid, "alice", v, "wait", h)
    denied("poll_not_running", e.apply.poll_complete, e.sid, "bob", v, "wait")


def test_poll_times_out_into_a_pause(e):
    clock = {"t": 1000.0}
    apply2 = ApplyEngine(e.store, e.ap, e.ex, e.guard, lambda sid: e.masker, audit=e.audit, clock=lambda: clock["t"])
    v, h = start(e, e.make_plan(POLL))
    e.ap.approve_step(e.sid, "alice", v, "wait", h)
    apply2.poll_once(e.sid, v, "wait", nick="alice")
    clock["t"] += 61
    out = apply2.poll_once(e.sid, v, "wait", nick="alice")
    assert out.state == "failed" and out.hint == "timeout" and e.store.session(e.sid)["state"] == "paused"
    assert e.apply.decide(e.sid, "alice", v, "wait", "retry") == "retry"            # 읽기 step이라 상태 확인 없이 재시도할 수 있다


def test_run_poll_loops_with_the_interval_and_stops_when_confirmed(e):
    v, h = start(e, e.make_plan(POLL))
    e.ap.approve_step(e.sid, "alice", v, "wait", h)
    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) == 3:
            e.apply.poll_complete(e.sid, "bob", v, "wait")

    e.apply.run_poll(e.sid, v, "wait", nick="alice", sleep=fake_sleep)
    assert sleeps == [5, 5, 5] and e.store.step_states(e.sid, v)["wait"]["state"] == "done"
    assert len([r for r in e.ex.ran if r.step_id == "wait"]) == 3


def test_a_failing_poll_command_stops_with_an_error(e):
    v, h = start(e, e.make_plan(POLL))
    e.ap.approve_step(e.sid, "alice", v, "wait", h)
    e.ex.script = [("ok", "{}", ""), ("failed", "", "ThrottlingException")]
    e.apply.poll_once(e.sid, v, "wait", nick="alice")
    out = e.apply.poll_once(e.sid, v, "wait", nick="alice")
    assert out.state == "failed" and out.hint == "throttled" and e.store.session(e.sid)["state"] == "paused"


# ---------------- 서버 재시작 ----------------
def test_after_a_restart_running_polls_resume_and_other_running_steps_are_interrupted(e):
    v, h = start(e, e.make_plan(POLL))
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    e.store.transition_step(e.sid, v, "reboot", "running")                          # 실행 중에 서버가 죽었다고 가정
    e.ap.approve_step(e.sid, "alice", v, "wait", h)
    e.apply.poll_once(e.sid, v, "wait", nick="alice")
    res = e.apply.recover_after_restart(e.sid, v)
    assert res == {"resumable": ["wait"], "interrupted": ["reboot"]}
    assert e.store.step_states(e.sid, v)["reboot"]["state"] == "failed" and e.store.session(e.sid)["state"] == "paused"
    denied("state_check_required", e.apply.decide, e.sid, "alice", v, "reboot", "retry")       # 적용됐는지 모르니 상태를 확인한 뒤에만


def test_recovery_does_nothing_when_the_session_is_not_applying(e):
    v = e.ap.submit_plan(e.sid, "alice", e.make_plan())
    assert e.apply.recover_after_restart(e.sid, v) == {"resumable": [], "interrupted": []}


# ---------------- 실제 모델 실행에서 발견: poll step이 확인 step인 경우 ----------------
def test_a_poll_step_used_as_the_check_starts_polling_instead_of_failing(e):
    """모델이 변경 step의 확인으로 poll step을 쓴 계획(실측). 자동 실행이 poll API를 쓰고, 완료는 사람이 확인한다."""
    ph = e.masker.mask("i-0abc123def4567890")
    steps = [{"id": "reboot", "kind": "cli", "title": "재부팅", "command": f"aws ec2 reboot-instances --instance-ids {ph}"},
             {"id": "wait", "kind": "poll", "title": "상태 대기", "check_for": "reboot", "command": f"aws ec2 describe-instance-status --instance-ids {ph}",
              "poll": {"interval_seconds": 5, "max_seconds": 60, "until": "ok"}}]
    from app.planner.schema import PlanDraft, build_plan
    plan = build_plan(PlanDraft.model_validate({"summary": "t", "phases": [{"id": "p1", "title": "x", "steps": steps}]}), e.guard, e.masker, postprocess=False)
    v, h = start(e, plan)
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    out = e.apply.run_step(e.sid, v, "reboot", nick="bob")
    assert out.state == "done" and [c.state for c in out.checks] == ["running"]
    assert e.store.step_states(e.sid, v)["wait"]["state"] == "running" and e.store.session(e.sid)["state"] == "applying"
    e.apply.poll_complete(e.sid, "bob", v, "wait")
    assert e.store.session(e.sid)["state"] == "completed"


def test_an_automatic_check_waits_for_its_own_dependencies(e):
    extra = ({"id": "pre", "kind": "cli", "title": "먼저", "command": "aws ec2 describe-vpcs"},)
    ph = e.masker.mask("i-0abc123def4567890")
    steps = [*extra,
             {"id": "reboot", "kind": "cli", "title": "재부팅", "command": f"aws ec2 reboot-instances --instance-ids {ph}"},
             {"id": "verify", "kind": "cli", "title": "확인", "check_for": "reboot", "depends_on": ["pre"],
              "command": f"aws ec2 describe-instance-status --instance-ids {ph}"}]
    from app.planner.schema import PlanDraft, build_plan
    plan = build_plan(PlanDraft.model_validate({"summary": "t", "phases": [{"id": "p1", "title": "x", "steps": steps}]}), e.guard, e.masker, postprocess=False)
    v, h = start(e, plan)
    e.ap.approve_step(e.sid, "bob", v, "reboot", h)
    out = e.apply.run_step(e.sid, v, "reboot", nick="bob")
    assert out.checks == () and e.store.step_states(e.sid, v)["verify"]["state"] == "pending"      # 의존 step이 끝나지 않아 자동 실행하지 않는다
    e.ap.approve_step(e.sid, "alice", v, "pre", h)
    e.apply.run_step(e.sid, v, "pre", nick="alice")
    e.ap.approve_step(e.sid, "alice", v, "verify", h)                                               # 이제 사람이 승인할 수 있다
    assert e.apply.run_step(e.sid, v, "verify", nick="alice").state == "done"
