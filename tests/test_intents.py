"""대화로 하는 진행 요청(D-49): 말은 실행 제안 카드가 되고, 누른 사람의 신원으로 정식 메시지가 처리되며 서버 규칙이 그대로 적용된다."""
import pytest
from test_apply import chunks, snapshot  # noqa: F401
from test_service import REQUEST, build, wait_for

from app.session.intents import parse_intent

IDS = {"reboot", "verify", "mk-sg"}


@pytest.mark.parametrize("text,kind,hint", [
    ("apply 진행해줘", "start_apply", None), ("Apply 시작해 줘", "start_apply", None), ("적용 시작하자", "start_apply", None),
    ("검토 요청해줘", "send_review", None), ("plan 올려줘", "send_review", None),
    ("확정해줘", "approve_plan", None), ("plan 승인해줘", "approve_plan", None), ("승인해줘", "approve_step", None), ("reboot 승인해줘", "approve_step", "reboot"),
    ("다음 절차 실행해줘", "run_step", None), ("reboot 실행해줘", "run_step", "reboot"), ("실행해줘", "run_step", None),
])
def test_progress_requests_become_intents(text, kind, hint):
    i = parse_intent(text, IDS)
    assert i is not None and i.kind == kind and i.step_hint == hint


@pytest.mark.parametrize("text", ["reboot 절차 지워줘", "두 번째 명령을 바꿔줘", "확인 절차 추가해줘", "제목 변경해줘", "EC2 하나 만들어줘", "", "고마워"])
def test_edits_and_other_talk_are_not_progress_requests(text):
    assert parse_intent(text, IDS) is None


@pytest.fixture()
def flow(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot)
    c = box.svc.create("alice", "진행 요청", REQUEST)
    sid = c["session_id"]
    box.svc.generate_plan(sid, REQUEST)
    box.svc.join(sid, c["invite"], "bob")
    yield box, sid
    box.store.close(); box.maps.close()


def say(box, sid, who, text):
    r = box.svc.handle(sid, who, {"type": "propose_edit", "text": text})
    assert r["ok"], r
    return box.svc.view(sid, who)


def last(v):
    return v["thread"][-1]["text"]


def test_plan_confirmation_is_a_card_only_a_non_requester_can_accept(flow):
    box, sid = flow
    v = say(box, sid, "alice", "확정해줘")
    assert v["proposal"] is None and "요청자는 plan을 확정(승인)할 수 없습니다" in last(v)               # 요청자에게는 카드가 아니라 안내
    v = say(box, sid, "bob", "확정해줘")
    p = v["proposal"]
    assert p["action"]["type"] == "approve_plan" and p["action"]["who"] == "approver" and v["session"]["state"] == "plan_review"   # 카드만 만들었고 아직 아무것도 바뀌지 않았다
    r = box.svc.handle(sid, "alice", {"type": "accept_proposal", "proposal": p["id"]})
    assert r["ok"] is False and box.svc.view(sid, "alice")["session"]["state"] == "plan_review"            # 요청자가 눌러도 서버가 거부하고
    assert box.svc.view(sid, "bob")["proposal"] is not None                                                 # 카드는 남는다
    assert box.svc.handle(sid, "bob", {"type": "accept_proposal", "proposal": p["id"]})["ok"]
    v = box.svc.view(sid, "bob")
    assert v["session"]["state"] == "plan_approved" and v["proposal"] is None and "bob님이 실행했습니다" in last(v)


def test_the_card_is_bound_to_the_plan_hash_it_showed(flow):
    box, sid = flow
    p = say(box, sid, "bob", "확정해줘")["proposal"]
    assert p["action"]["hash"] == box.svc.view(sid, "bob")["plan_hash"]


def test_apply_start_waits_for_the_reviewers_confirmation_then_runs_through_the_normal_checks(flow):
    box, sid = flow
    assert "확인자가 plan을 확정" in last(say(box, sid, "alice", "apply 진행해줘")) and box.svc.view(sid, "alice")["proposal"] is None
    box.svc.handle(sid, "bob", {"type": "accept_proposal", "proposal": say(box, sid, "bob", "확정해줘")["proposal"]["id"]})
    p = say(box, sid, "alice", "apply 진행해줘")["proposal"]
    assert p["action"]["type"] == "start_apply" and p["action"]["who"] == "requester"
    assert box.svc.handle(sid, "alice", {"type": "accept_proposal", "proposal": p["id"]})["ok"]
    assert box.svc.view(sid, "alice")["session"]["state"] == "applying"


def test_step_approval_and_run_by_talking_follow_the_four_eyes_rule(flow):
    box, sid = flow
    box.svc.handle(sid, "bob", {"type": "accept_proposal", "proposal": say(box, sid, "bob", "확정해줘")["proposal"]["id"]})
    box.svc.handle(sid, "alice", {"type": "accept_proposal", "proposal": say(box, sid, "alice", "apply 시작해줘")["proposal"]["id"]})
    assert "승인할 수 없습니다" in last(say(box, sid, "alice", "reboot 승인해줘"))                           # 요청자는 변경 절차를 승인할 수 없다
    pr = say(box, sid, "bob", "reboot 승인해줘")["proposal"]
    assert pr["action"]["type"] == "approve_step" and pr["action"]["step"] == "reboot"
    assert box.svc.handle(sid, "bob", {"type": "accept_proposal", "proposal": pr["id"]})["ok"]
    assert box.svc.view(sid, "bob")["steps"]["reboot"]["state"] == "approved"
    run = say(box, sid, "alice", "reboot 실행해줘")["proposal"]
    assert run["action"]["type"] == "run_step" and run["action"]["step"] == "reboot"
    assert box.svc.handle(sid, "alice", {"type": "accept_proposal", "proposal": run["id"]})["ok"]
    assert box.svc.view(sid, "alice")["steps"]["reboot"]["state"] == "done"


def test_an_unapproved_change_step_is_explained_and_the_next_runnable_read_is_offered(flow):
    box, sid = flow
    box.svc.handle(sid, "bob", {"type": "accept_proposal", "proposal": say(box, sid, "bob", "확정해줘")["proposal"]["id"]})
    box.svc.handle(sid, "alice", {"type": "accept_proposal", "proposal": say(box, sid, "alice", "apply 시작해줘")["proposal"]["id"]})
    v = say(box, sid, "alice", "reboot 실행해줘")
    assert v["proposal"] is None and "지금 실행할 수 있는 절차가 없습니다" in last(v) and "reboot" in last(v)       # 승인 전의 변경 절차는 실행 제안도 만들지 않는다
    nxt = say(box, sid, "alice", "다음 절차 실행해줘")["proposal"]
    assert nxt["action"]["type"] == "run_read" and nxt["action"]["step"] == "verify"                          # 확인 한 번으로 되는 읽기 절차는 제안한다


def test_review_request_needs_at_least_one_included_step(flow):
    box, sid = flow
    box.svc.handle(sid, "alice", {"type": "scratch", "op": "set_confirmed", "step_ids": ["reboot"], "value": False})
    v = say(box, sid, "alice", "검토 요청해줘")
    assert v["proposal"] is None and "포함한 절차가 없습니다" in last(v)


def test_input_guard_still_blocks_secrets_in_these_requests(flow):
    box, sid = flow
    assert box.svc.handle(sid, "alice", {"type": "propose_edit", "text": "password=hunter2 로 apply 진행해줘"})["ok"] is False
