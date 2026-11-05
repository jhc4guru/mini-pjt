"""말로 하는 연습장 수정: 모델은 수정안만 제안하고(스키마·누출·id 검증), 사람이 수락해야 기존 연습장 연산으로 적용된다."""
import json
import threading
from types import SimpleNamespace

import pytest
from test_apply import INSTANCE, chunks, snapshot  # noqa: F401
from test_service import build, wait_for

from app.guards.agent_limits import SchemaViolation
from app.guards.masking import Masker
from app.planner.scratch_edit import ScratchEditor


class FakeLLM:
    def __init__(self, *outputs):
        self.outputs, self.seen = list(outputs), []

    def complete(self, role, user, *, system=None, max_tokens=0, temperature=0.0, session_id=None):
        self.seen.append((role, user))
        return SimpleNamespace(text=self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0], requested_role=role, actual_role=role)


STEPS = [{"id": "a", "kind": "cli", "title": "VPC 조회", "command": "aws ec2 describe-vpcs", "confirmed": True, "phase": "p1"},
         {"id": "b", "kind": "cli", "title": "인스턴스 조회", "command": "aws ec2 describe-instances", "confirmed": False, "phase": "p1"}]


def propose(out, request="b 지워줘"):
    llm = FakeLLM(out)
    return ScratchEditor(llm, Masker()).propose(request, STEPS), llm


def j(summary="요약", ops=()):
    return json.dumps({"summary": summary, "ops": list(ops)}, ensure_ascii=False)


def test_a_valid_proposal_carries_only_allowed_operations_and_the_model_sees_masked_data_with_the_sub_role():
    p, llm = propose(j(ops=[{"op": "delete_step", "step_id": "b"}, {"op": "update_step", "step_id": "a", "fields": {"title": "VPC 목록"}}]))
    assert p.reasons == () and [o["op"] for o in p.ops] == ["delete_step", "update_step"]
    assert llm.seen[0][0] == "SUB" and "<scratchpad>" in llm.seen[0][1] and "<user_request>" in llm.seen[0][1]


@pytest.mark.parametrize("ops", [
    [{"op": "delete_step", "step_id": "zzz"}],                                                       # 없는 절차
    [{"op": "update_step", "step_id": "a", "fields": {"confirmed": "true"}}],                        # 확정은 제안할 수 없다
    [{"op": "update_step", "step_id": "a", "fields": {"title": "x", "check_for": "b"}}],             # 허용 필드 밖
    [{"op": "add_step", "step": {"kind": "iac", "title": "t", "command": "terraform apply"}}],       # kind 제한
    [{"op": "add_step", "step": {"kind": "cli", "title": "t", "command": ""}}],                      # 빈 명령
    [{"op": "move_step", "step_id": "a", "after": "nope"}],
])
def test_proposals_outside_the_server_rules_are_discarded_whole(ops):
    p, _ = propose(j(ops=ops))
    assert p.ops == () and p.reasons and p.reasons[0].startswith("invalid_proposal")


def test_operations_that_confirm_approve_or_run_do_not_exist():
    for bad in ("set_confirmed", "approve_step", "run_step", "delete_phase", "confirm_valid"):
        llm = FakeLLM(j(ops=[{"op": bad, "step_ids": ["a"]}]))
        with pytest.raises(SchemaViolation):
            ScratchEditor(llm, Masker()).propose("확정해줘", STEPS)


def test_a_proposal_with_a_real_value_or_an_invented_placeholder_is_dropped():
    p, _ = propose(j(ops=[{"op": "add_step", "step": {"kind": "cli", "title": "재부팅", "command": f"aws ec2 reboot-instances --instance-ids {INSTANCE}"}}]))
    assert p.ops == () and p.reasons == ("model_output_unsafe",)
    p2, _ = propose(j(ops=[{"op": "add_step", "step": {"kind": "cli", "title": "t", "command": "aws ec2 reboot-instances --instance-ids <INSTANCE_99>"}}]))
    assert p2.ops == () and p2.reasons == ("model_output_unsafe",)


def test_no_change_is_reported_without_operations():
    p, _ = propose(j(summary="무엇을 고칠지 모호합니다", ops=[]))
    assert p.ops == () and p.reasons == ("no_change",) and "모호" in p.summary


# ---------------- 서비스: 제안 → 수락/무시 ----------------
@pytest.fixture()
def pad(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot)
    c = box.svc.create("alice", "수정 작업")
    sid = c["session_id"]
    box.svc.join(sid, c["invite"], "bob")
    for t, cmd in (("VPC 조회", "aws ec2 describe-vpcs"), ("인스턴스 조회", "aws ec2 describe-instances")):
        assert box.svc.handle(sid, "alice", {"type": "scratch", "op": "add_step", "step": {"kind": "cli", "title": t, "command": cmd}})["ok"]
    yield box, sid
    for t in threading.enumerate():
        if t.name.startswith("edit-"):
            t.join(timeout=10)
    box.store.close(); box.maps.close()


def use(box, *outputs):
    llm = FakeLLM(*outputs)
    box.svc._editor_factory = lambda masker: ScratchEditor(llm, masker)
    return llm


def titles(box, sid):
    return [s["title"] for p in box.svc.view(sid, "alice")["scratch"]["phases"] for s in p["steps"]]


def test_a_proposal_changes_nothing_until_someone_accepts_it_and_the_accept_can_be_undone(pad):
    box, sid = pad
    use(box, j("두 번째 절차를 지웁니다", [{"op": "delete_step", "step_id": "step-2"}]))
    assert box.svc.handle(sid, "bob", {"type": "propose_edit", "text": "인스턴스 조회 지워줘"})["ok"]
    assert wait_for(lambda: box.svc.view(sid, "alice")["proposal"] is not None)
    v = box.svc.view(sid, "alice")
    assert titles(box, sid) == ["VPC 조회", "인스턴스 조회"] and v["proposal"]["by"] == "bob" and v["proposing"] is False        # 수락 전에는 그대로
    pid = v["proposal"]["id"]
    r = box.svc.handle(sid, "alice", {"type": "accept_proposal", "proposal": pid})
    assert r["ok"] and r["applied"] == 1 and titles(box, sid) == ["VPC 조회"] and box.svc.view(sid, "alice")["proposal"] is None
    assert box.svc.handle(sid, "alice", {"type": "scratch", "op": "undo"})["ok"] and titles(box, sid) == ["VPC 조회", "인스턴스 조회"]
    assert box.svc.handle(sid, "alice", {"type": "accept_proposal", "proposal": pid})["ok"] is False                     # 한 번만 수락할 수 있다


def test_a_dismissed_proposal_leaves_the_pad_untouched(pad):
    box, sid = pad
    use(box, j("지움", [{"op": "delete_step", "step_id": "step-1"}]))
    box.svc.handle(sid, "alice", {"type": "propose_edit", "text": "첫 절차 지워줘"})
    assert wait_for(lambda: box.svc.view(sid, "alice")["proposal"] is not None)
    pid = box.svc.view(sid, "alice")["proposal"]["id"]
    assert box.svc.handle(sid, "bob", {"type": "dismiss_proposal", "proposal": pid})["ok"]
    assert titles(box, sid) == ["VPC 조회", "인스턴스 조회"] and box.svc.view(sid, "alice")["proposal"] is None


def test_a_proposal_that_fails_midway_is_rolled_back_as_a_whole(pad):
    box, sid = pad
    use(box, j("두 가지", [{"op": "update_step", "step_id": "step-1", "fields": {"title": "바뀐 제목"}}, {"op": "delete_step", "step_id": "step-2"}]))
    box.svc.handle(sid, "alice", {"type": "propose_edit", "text": "고쳐줘"})
    assert wait_for(lambda: box.svc.view(sid, "alice")["proposal"] is not None)
    pid = box.svc.view(sid, "alice")["proposal"]["id"]
    box.svc.handle(sid, "alice", {"type": "scratch", "op": "delete_step", "step_id": "step-2"})                       # 그 사이 사람이 두 번째 절차를 지웠다
    r = box.svc.handle(sid, "alice", {"type": "accept_proposal", "proposal": pid})
    assert r["ok"] and r["applied"] == 0 and titles(box, sid) == ["VPC 조회"]                                           # 첫 연산도 되돌려 전부 아니면 전무
    assert any("적용하지 못했습니다" in m["text"] for m in box.svc.view(sid, "alice")["thread"])


def test_an_unusable_proposal_is_explained_and_the_input_guard_still_applies(pad):
    box, sid = pad
    use(box, j("모호", []))
    box.svc.handle(sid, "alice", {"type": "propose_edit", "text": "알아서 해줘"})
    assert wait_for(lambda: any("모호" in m["text"] for m in box.svc.view(sid, "alice")["thread"]))
    assert box.svc.view(sid, "alice")["proposal"] is None
    r = box.svc.handle(sid, "alice", {"type": "propose_edit", "text": "password=hunter2 로 바꿔줘"})                       # 시크릿은 입력 가드가 막는다(모델 호출 전)
    assert r["ok"] is False


def test_it_needs_an_editor_a_participant_and_something_to_edit(pad):
    box, sid = pad
    assert box.svc.handle(sid, "alice", {"type": "propose_edit", "text": "지워줘"})["code"] == "edit_unavailable"
    use(box, j("x", []))
    assert box.svc.handle(sid, "mallory", {"type": "propose_edit", "text": "지워줘"})["ok"] is False
