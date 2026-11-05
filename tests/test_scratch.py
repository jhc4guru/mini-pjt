"""연습장(초안): 틀려도 저장되고 상태만 알려 주며, 확정한 절차만 Apply로 가고, 서버가 넣은 절차도 고치고 지울 수 있고, 잠긴 절차는 못 건드린다."""
import json
import threading

import pytest
from test_apply import INSTANCE, chunks, snapshot  # noqa: F401
from test_service import REQUEST, build, started_apply, wait_for

from app.rag.retriever import HybridRetriever


def S(box, sid, nick, **m):
    return box.svc.handle(sid, nick, {"type": "scratch", **m})


def sc(box, sid, nick="alice"):
    return box.svc.view(sid, nick)["scratch"]


def steps(box, sid, nick="alice"):
    return {x["id"]: x for p in sc(box, sid, nick)["phases"] for x in p["steps"]}


@pytest.fixture()
def empty(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, catalog_retriever=HybridRetriever(chunks, vector_index=None))
    c = box.svc.create("alice", "빈 작업")                                                     # 요청 없이 제목만: 빈 plan 모드
    sid = c["session_id"]
    box.svc.join(sid, c["invite"], "bob")
    yield box, sid
    box.store.close(); box.maps.close()


@pytest.fixture()
def generated(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot)
    c = box.svc.create("alice", "에이전트 작업", REQUEST)
    sid = c["session_id"]
    box.svc.generate_plan(sid, REQUEST)
    box.svc.join(sid, c["invite"], "bob")
    yield box, sid
    box.store.close(); box.maps.close()


# ---------------- 빈 plan 모드 ----------------
def test_an_empty_plan_starts_from_a_title_only_without_calling_the_model(empty):
    box, sid = empty
    v = box.svc.view(sid, "alice")
    assert v["session"]["state"] == "draft" and v["session"]["title"] == "빈 작업" and v["session"]["request"] == "" and v.get("plan") is None
    assert v["pipeline"]["status"] == "manual" and v["scratch"]["phases"] == [] and box.pipelines == []
    assert any("빈 plan" in m["text"] for m in v["thread"] if m["role"] == "assistant")
    assert box.svc.handle(sid, "alice", {"type": "send_to_apply"})["code"] == "nothing_confirmed"              # 확정한 것이 없으면 보낼 수 없다


def test_a_manual_plan_goes_from_draft_to_confirmed_to_apply(empty):
    box, sid = empty
    r = S(box, sid, "alice", op="add_steps", steps=[
        {"id": "stop", "kind": "cli", "title": "인스턴스 중지", "command": f"aws ec2 stop-instances --instance-ids {INSTANCE}"},
        {"id": "stop-chk", "kind": "cli", "title": "중지 확인", "command": f"aws ec2 describe-instance-status --instance-ids {INSTANCE}", "check_for": "stop"}])
    assert r["ok"] and r["added"] == ["stop", "stop-chk"]
    st = steps(box, sid)
    assert st["stop"]["status"] == "ok" and st["stop"]["op"] == "mutate" and st["stop-chk"]["status"] == "ok" and not st["stop"]["confirmed"]
    assert INSTANCE not in json.dumps(box.store.get_meta(sid, "scratch"))                                      # 저장된 초안은 마스킹된 텍스트
    c = S(box, sid, "alice", op="set_confirmed", step_ids=["stop"], value=True)
    assert c["ok"] and c["confirmed"] == ["stop", "stop-chk"]                                                  # 변경 절차를 확정하면 확인 절차가 함께 확정된다
    assert box.svc.handle(sid, "alice", {"type": "send_to_apply"})["ok"]
    v = box.svc.view(sid, "alice")
    assert v["session"]["state"] == "plan_review" and v["version"] == 1 and [x["id"] for p in v["plan"]["phases"] for x in p["steps"]][:2] == ["stop", "stop-chk"] \
        or "stop" in [x["id"] for p in v["plan"]["phases"] for x in p["steps"]]
    assert INSTANCE in json.dumps(v["plan"], ensure_ascii=False) and v["scratch"]["dirty"] is False


# ---------------- 틀려도 저장되고 상태를 알려 준다 ----------------
def test_a_draft_with_a_missing_required_option_is_saved_and_flagged_but_cannot_be_confirmed(empty):
    box, sid = empty
    assert S(box, sid, "alice", op="add_step", step={"id": "launch", "kind": "cli", "title": "EC2 생성", "command": "aws ec2 run-instances --instance-type t3.nano"})["ok"]
    x = steps(box, sid)["launch"]
    assert x["status"] == "warn" and {"code": "missing_param", "detail": "--image-id", "sev": "warn"} in x["issues"]
    assert any(i["code"] == "missing_check" for i in x["issues"])                                              # 확인 절차도 없다
    r = S(box, sid, "alice", op="set_confirmed", step_ids=["launch"], value=True)
    assert r["ok"] is False and r["code"] == "not_confirmable:launch" and "missing_param" in r["codes"]
    S(box, sid, "alice", op="update_step", step_id="launch", fields={"command": "aws ec2 run-instances --image-id ami-0123456789abcdef0 --instance-type t3.nano"})
    assert [i["code"] for i in steps(box, sid)["launch"]["issues"]] == ["missing_check"]                       # 필수값은 채워졌고 확인 절차만 남음
    S(box, sid, "alice", op="add_step", after="launch", step={"id": "launch-chk", "kind": "cli", "title": "확인", "command": "aws ec2 describe-instances", "check_for": "launch"})
    assert steps(box, sid)["launch"]["status"] == "ok"
    assert S(box, sid, "alice", op="set_confirmed", step_ids=["launch"], value=True)["ok"]


def test_blocked_commands_are_saved_as_errors_and_secrets_in_text_are_refused(empty):
    box, sid = empty
    assert S(box, sid, "alice", op="add_step", step={"id": "bad", "kind": "cli", "title": "t", "command": "aws ec2 describe-vpcs --profile prod"})["ok"]
    x = steps(box, sid)["bad"]
    assert x["status"] == "err" and any(i["code"] == "blocked" for i in x["issues"])
    assert S(box, sid, "alice", op="set_confirmed", step_ids=["bad"], value=True)["code"].startswith("not_confirmable:bad")
    r = S(box, sid, "alice", op="add_step", step={"kind": "cli", "title": "password=hunter2", "command": "aws ec2 describe-vpcs"})
    assert r["ok"] is False and r["code"].startswith("input_blocked:title") and "hunter2" not in json.dumps(r)
    assert S(box, sid, "mallory", op="add_step", step={})["code"] == "not_a_participant"


def test_a_mutating_draft_needs_its_check_and_the_group_is_confirmed_and_unconfirmed_together(empty):
    box, sid = empty
    S(box, sid, "alice", op="add_steps", steps=[
        {"id": "a", "kind": "cli", "title": "조회", "command": "aws ec2 describe-vpcs"},
        {"id": "m", "kind": "cli", "title": "중지", "command": f"aws ec2 stop-instances --instance-ids {INSTANCE}", "depends_on": ["a"]},
        {"id": "c", "kind": "cli", "title": "확인", "command": f"aws ec2 describe-instance-status --instance-ids {INSTANCE}", "check_for": "m"}])
    assert S(box, sid, "alice", op="set_confirmed", step_ids=["m"], value=True)["confirmed"] == ["a", "c", "m"]       # 의존(a)과 확인(c)이 함께
    r = S(box, sid, "alice", op="set_confirmed", step_ids=["c"], value=False)
    assert r["unconfirmed"] == ["c", "m"]                                                                       # 확인 절차를 풀면 그 변경 절차도 풀린다
    assert [i for i, x in steps(box, sid).items() if x["confirmed"]] == ["a"]
    S(box, sid, "alice", op="set_confirmed", step_ids=["m"], value=True)
    r = S(box, sid, "alice", op="set_confirmed", step_ids=["a"], value=False)
    assert set(r["unconfirmed"]) == {"a", "m", "c"}                                                             # 의존 대상을 풀면 의존하는 쪽도 푼다
    assert S(box, sid, "alice", op="confirm_valid")["confirmed"] == ["a", "c", "m"]                              # 가능한 것을 한꺼번에


def test_editing_a_confirmed_steps_command_unconfirms_it_and_its_group(empty):
    box, sid = empty
    S(box, sid, "alice", op="add_steps", steps=[
        {"id": "m", "kind": "cli", "title": "중지", "command": f"aws ec2 stop-instances --instance-ids {INSTANCE}"},
        {"id": "c", "kind": "cli", "title": "확인", "command": f"aws ec2 describe-instance-status --instance-ids {INSTANCE}", "check_for": "m"}])
    S(box, sid, "alice", op="set_confirmed", step_ids=["m"], value=True)
    r = S(box, sid, "alice", op="update_step", step_id="m", fields={"title": "제목만 바꿈"})
    assert r["unconfirmed"] == [] and steps(box, sid)["m"]["confirmed"]                                         # 제목만 바꾸면 확정은 그대로
    r = S(box, sid, "alice", op="update_step", step_id="m", fields={"command": f"aws ec2 start-instances --instance-ids {INSTANCE}"})
    assert set(r["unconfirmed"]) == {"m", "c"} and not steps(box, sid)["m"]["confirmed"] and not steps(box, sid)["c"]["confirmed"]


# ---------------- Apply로 보내기 ----------------
def test_only_confirmed_steps_go_to_apply_and_server_added_steps_appear_in_the_scratchpad(empty):
    box, sid = empty
    S(box, sid, "alice", op="add_steps", steps=[
        {"id": "m", "kind": "cli", "title": "재부팅", "command": f"aws ec2 reboot-instances --instance-ids {INSTANCE}"},
        {"id": "c", "kind": "cli", "title": "확인", "command": f"aws ec2 describe-instance-status --instance-ids {INSTANCE}", "check_for": "m"},
        {"id": "later", "kind": "cli", "title": "나중에", "command": "aws ec2 describe-vpcs"}])
    S(box, sid, "alice", op="set_confirmed", step_ids=["m"], value=True)
    assert box.svc.handle(sid, "alice", {"type": "send_to_apply"})["ok"]
    v = box.svc.view(sid, "alice")
    sent = [x["id"] for p in v["plan"]["phases"] for x in p["steps"]]
    assert "later" not in sent and {"m", "c"} <= set(sent) and any(i.startswith("pre-") for i in sent)            # 확정한 것만, 서버가 사전 점검을 넣음
    st = steps(box, sid)
    assert "later" in st and st["later"]["confirmed"] is False                                                  # 연습장에는 남아 있다
    pre = next(x for i, x in st.items() if i.startswith("pre-"))
    assert pre["origin"] == "auto" and pre["confirmed"] is True and "pre-m" in st["m"]["depends_on"]       # 서버가 넣은 절차가 연습장에도 나타난다


def test_a_server_added_step_can_be_deleted_and_is_not_inserted_again(empty):
    box, sid = empty
    S(box, sid, "alice", op="add_steps", steps=[
        {"id": "m", "kind": "cli", "title": "재부팅", "command": f"aws ec2 reboot-instances --instance-ids {INSTANCE}"},
        {"id": "c", "kind": "cli", "title": "확인", "command": f"aws ec2 describe-instance-status --instance-ids {INSTANCE}", "check_for": "m"}])
    S(box, sid, "alice", op="set_confirmed", step_ids=["m"], value=True)
    box.svc.handle(sid, "alice", {"type": "send_to_apply"})
    assert "pre-m" in steps(box, sid)
    assert S(box, sid, "alice", op="delete_step", step_id="pre-m")["ok"]                                        # 제안(서버 추가) 절차도 지울 수 있다
    S(box, sid, "alice", op="update_step", step_id="m", fields={"depends_on": []})
    S(box, sid, "alice", op="set_confirmed", step_ids=["m"], value=True)
    assert box.svc.handle(sid, "alice", {"type": "send_to_apply"})["ok"]
    sent = [x["id"] for p in box.svc.view(sid, "alice")["plan"]["phases"] for x in p["steps"]]
    assert "pre-m" not in sent and "pre-m" not in steps(box, sid)                                               # 다시 끼워 넣지 않는다


def test_the_scratchpad_knows_when_it_differs_from_the_plan_that_was_sent(empty):
    box, sid = empty
    S(box, sid, "alice", op="add_step", step={"id": "r", "kind": "cli", "title": "조회", "command": "aws ec2 describe-vpcs"})
    S(box, sid, "alice", op="set_confirmed", step_ids=["r"], value=True)
    assert sc(box, sid)["dirty"] is True                                                                        # 아직 보내지 않았다
    box.svc.handle(sid, "alice", {"type": "send_to_apply"})
    assert sc(box, sid)["dirty"] is False
    S(box, sid, "alice", op="update_step", step_id="r", fields={"title": "다른 제목"})
    assert sc(box, sid)["dirty"] is True                                                                        # 고치면 다시 보내야 한다
    box.svc.handle(sid, "alice", {"type": "send_to_apply"})
    v = box.svc.view(sid, "alice")
    assert v["version"] == 2 and v["session"]["state"] == "plan_review" and sc(box, sid)["dirty"] is False


def test_sending_validates_the_whole_plan_again(empty):
    """확정 단계의 검증은 단위별이다. 보내는 순간 `build_plan`이 전체를 다시 검증한다(가드는 약해지지 않는다)."""
    box, sid = empty
    S(box, sid, "alice", op="add_step", step={"id": "r", "kind": "cli", "title": "조회", "command": "aws ec2 describe-vpcs"})
    S(box, sid, "alice", op="set_confirmed", step_ids=["r"], value=True)
    doc = json.loads(box.store.get_meta(sid, "scratch"))
    doc["phases"][0]["steps"][0]["command"] = "aws ec2 describe-vpcs --profile prod"                           # DB를 직접 바꿔 가드에 걸리는 명령을 확정 상태로 만든다
    box.store.set_meta(sid, "scratch", json.dumps(doc))
    r = box.svc.handle(sid, "alice", {"type": "send_to_apply"})
    assert r["ok"] is False and r["code"] == "plan_invalid" and any(c.startswith("blocked_command:r") for c in r["codes"])
    assert box.svc.view(sid, "alice").get("plan") is None


# ---------------- 에이전트 plan과 연습장 ----------------
def test_an_agent_plan_fills_the_scratchpad_as_confirmed_and_can_be_edited_and_deleted(generated):
    box, sid = generated
    st = steps(box, sid)
    assert set(st) == {"reboot", "verify"} and all(x["confirmed"] and x["origin"] == "agent" and x["status"] == "ok" for x in st.values())
    assert S(box, sid, "alice", op="update_step", step_id="reboot", fields={"title": "제안을 고침"})["ok"]
    assert S(box, sid, "alice", op="delete_step", step_id="verify")["unconfirmed"] == ["reboot"]               # 확인을 지우면 변경의 확정도 풀린다
    st = steps(box, sid)
    assert "verify" not in st and st["reboot"]["confirmed"] is False and any(i["code"] == "missing_check" for i in st["reboot"]["issues"])
    assert sc(box, sid)["dirty"] is True


def test_steps_can_move_between_phases_and_be_duplicated(empty):
    box, sid = empty
    S(box, sid, "alice", op="add_step", step={"id": "a", "kind": "cli", "title": "A", "command": "aws ec2 describe-vpcs"})
    ph = S(box, sid, "alice", op="add_phase", title="두번째")
    assert ph["phase"] == "p2"
    S(box, sid, "alice", op="add_step", phase="p2", step={"id": "b", "kind": "cli", "title": "B", "command": "aws ec2 describe-subnets"})
    assert S(box, sid, "alice", op="move_step", step_id="a", phase="p2", after="b")["ok"]                      # phase 사이 이동
    v = sc(box, sid)
    assert [x["id"] for x in v["phases"][0]["steps"]] == [] and [x["id"] for x in v["phases"][1]["steps"]] == ["b", "a"]
    assert S(box, sid, "alice", op="move_step", step_id="a", phase="p2", after=None)["ok"]
    assert [x["id"] for x in sc(box, sid)["phases"][1]["steps"]] == ["a", "b"]
    d = S(box, sid, "alice", op="duplicate_step", step_id="a")
    copy = d["added"][0]
    assert steps(box, sid)[copy]["title"] == "A (복제)" and steps(box, sid)[copy]["confirmed"] is False
    assert S(box, sid, "alice", op="update_phase", phase="p2", fields={"title": "이름 바꿈", "gate": True})["ok"] and sc(box, sid)["phases"][1]["title"] == "이름 바꿈"
    assert S(box, sid, "alice", op="delete_phase", phase="p2")["ok"] and sc(box, sid)["phases"][1:] == []


def test_undo_and_redo_restore_earlier_drafts(empty):
    box, sid = empty
    S(box, sid, "alice", op="add_step", step={"id": "a", "kind": "cli", "title": "A", "command": "aws ec2 describe-vpcs"})
    S(box, sid, "alice", op="update_step", step_id="a", fields={"title": "바뀜"})
    assert sc(box, sid)["undo"] is True and sc(box, sid)["redo"] is False
    assert S(box, sid, "alice", op="undo")["ok"] and steps(box, sid)["a"]["title"] == "A" and sc(box, sid)["redo"] is True
    assert S(box, sid, "alice", op="undo")["ok"] and steps(box, sid) == {}
    assert S(box, sid, "alice", op="undo")["code"] == "nothing_to_undo"
    assert S(box, sid, "alice", op="redo")["ok"] and S(box, sid, "alice", op="redo")["ok"] and steps(box, sid)["a"]["title"] == "바뀜"
    S(box, sid, "alice", op="undo")
    S(box, sid, "alice", op="add_step", step={"id": "z", "kind": "cli", "title": "Z", "command": "aws ec2 describe-vpcs"})      # 새로 고치면 다시 실행 기록은 지워진다
    assert sc(box, sid)["redo"] is False


# ---------------- 잠금 ----------------
def test_confirmed_steps_cannot_be_edited_while_applying_but_drafts_can(generated):
    box, sid = generated
    S(box, sid, "alice", op="add_step", step={"id": "idea", "kind": "cli", "title": "아이디어", "command": "aws ec2 describe-vpcs"})
    h = box.svc.view(sid, "alice")["plan_hash"]
    box.svc.handle(sid, "bob", {"type": "approve_plan", "version": 1, "hash": h})
    box.svc.handle(sid, "alice", {"type": "start_apply"})
    r = S(box, sid, "alice", op="update_step", step_id="reboot", fields={"title": "바꾸기"})
    assert r == {"ok": False, "code": "step_in_apply"}
    assert S(box, sid, "alice", op="delete_step", step_id="verify")["code"] == "step_in_apply"
    assert S(box, sid, "alice", op="update_step", step_id="idea", fields={"title": "아직 연습장"})["ok"]          # 확정하지 않은 초안은 자유롭다
    assert steps(box, sid)["reboot"]["locked"] == "in_apply"
    assert box.svc.handle(sid, "alice", {"type": "send_to_apply"})["code"] == "send_not_allowed"                 # apply 진행 중에는 보낼 수 없다


def test_after_pausing_done_steps_are_locked_and_the_rest_can_be_resent_as_a_replan(generated):
    box, sid = generated
    h = box.svc.view(sid, "alice")["plan_hash"]
    box.svc.handle(sid, "bob", {"type": "approve_plan", "version": 1, "hash": h})
    box.svc.handle(sid, "alice", {"type": "start_apply"})
    box.svc.handle(sid, "bob", {"type": "approve_step", "version": 1, "step": "reboot", "hash": h})
    box.ex.script = [("ok", "{}", "")]
    box.svc.handle(sid, "bob", {"type": "run_step", "version": 1, "step": "reboot"})                            # reboot 끝, verify는 자동 실행되어 세션이 완료될 수 있다
    sess = box.svc.view(sid, "alice")["session"]["state"]
    assert sess == "completed"                                                                                  # 두 절차가 모두 끝나 세션이 닫힌다
    assert S(box, sid, "alice", op="add_step", step={"kind": "cli", "title": "x", "command": "aws ec2 describe-vpcs"})["code"] == "session_closed"


def test_a_paused_session_resends_only_changes_to_unfinished_steps(tmp_path, chunks, snapshot):
    extra = ({"id": "later", "kind": "cli", "title": "나중 조회", "command": "aws ec2 describe-vpcs"},)
    box = build(tmp_path, chunks, snapshot, extra=extra)
    c = box.svc.create("alice", "일시정지 작업", REQUEST)
    sid = c["session_id"]
    box.svc.generate_plan(sid, REQUEST)
    box.svc.join(sid, c["invite"], "bob")
    h = box.svc.view(sid, "alice")["plan_hash"]
    box.svc.handle(sid, "bob", {"type": "approve_plan", "version": 1, "hash": h})
    box.svc.handle(sid, "alice", {"type": "start_apply"})
    box.svc.handle(sid, "bob", {"type": "approve_step", "version": 1, "step": "reboot", "hash": h})
    box.svc.handle(sid, "bob", {"type": "run_step", "version": 1, "step": "reboot"})                            # reboot, verify 끝
    assert box.svc.handle(sid, "alice", {"type": "pause"})["ok"]
    st = steps(box, sid)
    assert st["reboot"]["locked"] == "done" and st["verify"]["locked"] == "done" and st["later"]["locked"] == ""
    assert S(box, sid, "alice", op="update_step", step_id="reboot", fields={"title": "x"})["code"] == "step_locked_done"
    assert S(box, sid, "alice", op="update_step", step_id="later", fields={"title": "나중 조회(수정)"})["ok"]
    S(box, sid, "alice", op="set_confirmed", step_ids=["later"], value=True)
    assert box.svc.handle(sid, "alice", {"type": "send_to_apply"})["ok"]                                        # 부분 재계획: 완료 절차는 그대로
    v = box.svc.view(sid, "alice")
    assert v["session"]["state"] == "plan_review" and v["version"] == 2 and v["steps"]["reboot"]["state"] == "done" and v["steps"]["later"]["state"] == "pending"
    box.store.close(); box.maps.close()


# ---------------- 명령 만들기 도우미 ----------------
def test_catalog_search_returns_commands_with_required_options_and_examples(empty):
    box, sid = empty
    r = box.svc.handle(sid, "alice", {"type": "catalog_search", "query": "인스턴스 생성 run instances"})
    assert r["ok"] and r["items"]
    run = next(i for i in r["items"] if i["id"] == "aws:ec2:run-instances")
    assert run["required"] == ["--image-id"] and "--instance-type" in run["optional"] and run["kind"] == "mutate" and run["command"] == "aws ec2 run-instances"
    assert box.svc.handle(sid, "alice", {"type": "catalog_search", "query": "password=hunter2"})["code"] == "input_rejected"
    assert box.svc.handle(sid, "mallory", {"type": "catalog_search", "query": "x"})["code"] == "not_a_participant"


def test_suggestions_come_from_the_inventory_by_option_and_lookups_run_read_commands(empty):
    box, sid = empty
    assert box.svc.handle(sid, "alice", {"type": "suggest", "option": "--cluster-name"})["names"] == ["prod-db", "dev-main"]
    assert box.svc.handle(sid, "alice", {"type": "suggest", "option": "--auto-scaling-group-name"})["names"] == ["eks-workers-a"]
    assert box.svc.handle(sid, "alice", {"type": "suggest", "option": "--unknown"})["names"] == []
    box.ex.script = [("ok", json.dumps({"Vpcs": [{"VpcId": "vpc-0abc1234def567890", "IsDefault": True, "CidrBlock": "172.31.0.0/16"}]}), "")]
    r = box.svc.handle(sid, "alice", {"type": "lookup", "kind": "vpc"})
    assert r["ok"] and r["items"][0]["value"] == "vpc-0abc1234def567890" and "기본 VPC" in r["items"][0]["label"]
    box.ex.script = [("ok", json.dumps({"Subnets": [{"SubnetId": "subnet-0abc1234def567890", "AvailabilityZone": "us-east-1a", "DefaultForAz": True, "CidrBlock": "172.31.0.0/20"}]}), "")]
    r = box.svc.handle(sid, "alice", {"type": "lookup", "kind": "subnet", "vpc": "vpc-0abc1234def567890"})
    assert r["items"][0]["value"] == "subnet-0abc1234def567890" and "vpc-id,Values=vpc-0abc1234def567890" in box.ex.ran[-1].command
    box.ex.script = [("ok", json.dumps({"Parameter": {"Value": "ami-0aaaabbbbccccdddd"}}), "")]
    assert box.svc.handle(sid, "alice", {"type": "lookup", "kind": "ami_ubuntu"})["items"][0]["value"] == "ami-0aaaabbbbccccdddd"
    box.ex.script = [("ok", "not json", "")]
    assert box.svc.handle(sid, "alice", {"type": "lookup", "kind": "sg"})["code"] == "lookup_failed"
    assert box.svc.handle(sid, "alice", {"type": "lookup", "kind": "nope"})["code"] == "unknown_lookup"
    assert all(r.kind == "read" for r in box.ex.ran)                                                              # 조회는 읽기 명령뿐이다


def test_lookup_results_never_reach_the_stored_data(empty):
    box, sid = empty
    box.ex.script = [("ok", json.dumps({"Vpcs": [{"VpcId": "vpc-0abc1234def567890", "IsDefault": True, "CidrBlock": "172.31.0.0/16"}]}), "")]
    box.svc.handle(sid, "alice", {"type": "lookup", "kind": "vpc"})
    assert "vpc-0abc1234def567890" not in json.dumps([e for e in box.store.events(sid)]) and "vpc-0abc1234def567890" not in (box.store.get_meta(sid, "scratch") or "")


# ---------------- 빈 plan 모드에서 에이전트에게 초안 요청 ----------------
def test_in_an_empty_plan_the_agent_adds_unconfirmed_drafts_without_touching_the_users_work(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, statuses=["plan"])
    c = box.svc.create("alice", "빈 작업")
    sid = c["session_id"]
    S(box, sid, "alice", op="add_step", step={"id": "reboot", "kind": "cli", "title": "내가 만든 절차", "command": "aws ec2 describe-vpcs"})      # 에이전트 plan과 같은 id
    S(box, sid, "alice", op="set_confirmed", step_ids=["reboot"], value=True)
    assert box.svc.handle(sid, "alice", {"type": "answer", "text": "인스턴스 재부팅 계획을 초안으로 만들어줘"})["ok"]
    assert wait_for(lambda: any("초안" in m["text"] and m["role"] == "assistant" for m in box.svc.view(sid, "alice")["thread"][1:]))
    v = box.svc.view(sid, "alice")
    assert v.get("plan") is None and v["session"]["state"] == "draft" and v["pipeline"]["status"] == "manual"                # plan으로 제출하지 않는다
    st = steps(box, sid)
    assert st["reboot"]["title"] == "내가 만든 절차" and st["reboot"]["confirmed"] is True                                    # 내 절차는 그대로
    new = {i: x for i, x in st.items() if i != "reboot"}
    assert {"reboot-g2", "verify"} <= set(new) and all(x["confirmed"] is False and x["origin"] == "agent" for x in new.values())      # 겹치는 id는 바뀌고 초안은 확정되지 않았다
    assert st["verify"]["check_for"] == "reboot-g2"                                                                          # 참조도 함께 바뀐다
    for t in threading.enumerate():                                                                                          # 생성 스레드가 끝난 뒤에 DB를 닫는다(메시지가 보인 뒤에도 상태를 기록한다)
        if t.name.startswith("plan-"):
            t.join(timeout=10)
    box.store.close(); box.maps.close()


# ---------------- 직접 입력·요청 세션의 연습장·중단 안내 ----------------
def _join_generation_threads():
    for t in threading.enumerate():
        if t.name.startswith("plan-"):
            t.join(timeout=10)


def test_a_blank_step_can_be_added_and_saved_but_not_confirmed(empty):
    box, sid = empty
    r = S(box, sid, "alice", op="add_step", step={"kind": "cli", "title": "새 절차", "command": ""})
    assert r["ok"] and len(r["added"]) == 1
    st = steps(box, sid)[r["added"][0]]
    assert st["status"] == "err" and {i["code"] for i in st["issues"]} == {"command_required"}
    assert S(box, sid, "alice", op="set_confirmed", step_ids=[st["id"]], value=True)["ok"] is False              # 확정할 수 없다
    S(box, sid, "alice", op="update_step", step_id=st["id"], fields={"command": "aws ec2 run-instances --instance-type t3.micro"})
    codes = {i["code"] for i in steps(box, sid)[st["id"]]["issues"]}
    assert "missing_param" in codes and steps(box, sid)[st["id"]]["status"] == "warn"                          # 빠진 필수 옵션은 ⚠로만 알려 준다


def test_the_scratchpad_works_while_the_agent_is_asking_and_its_plan_then_arrives_as_drafts(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, statuses=["clarify", "plan"])
    c = box.svc.create("alice", "되묻는 작업", REQUEST)
    sid = c["session_id"]
    box.svc.generate_plan(sid, REQUEST)                                                                          # 첫 응답은 되묻기
    assert box.svc.view(sid, "alice")["pipeline"]["status"] == "clarify"
    r = S(box, sid, "alice", op="add_step", step={"kind": "cli", "title": "내가 먼저 쓴 절차", "command": ""})           # 되묻는 중에도 연습장을 쓴다
    assert r["ok"]
    assert box.svc.handle(sid, "alice", {"type": "answer", "text": "서버는 하나입니다"})["ok"]
    assert wait_for(lambda: any("초안" in m["text"] for m in box.svc.view(sid, "alice")["thread"]))
    _join_generation_threads()
    v = box.svc.view(sid, "alice")
    assert v.get("plan") is None and v["pipeline"]["status"] == "manual" and v["session"]["state"] == "draft"        # plan으로 제출하지 않고 초안으로만
    st = steps(box, sid)
    assert st[r["added"][0]]["title"] == "내가 먼저 쓴 절차" and any(x["origin"] == "agent" and x["confirmed"] is False for x in st.values())
    box.store.close(); box.maps.close()


def test_an_agent_question_in_an_empty_plan_keeps_the_pad_open_and_the_reply_continues_the_conversation(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, statuses=["clarify", "plan"])
    c = box.svc.create("alice", "빈 작업")
    sid = c["session_id"]
    assert box.svc.handle(sid, "alice", {"type": "answer", "text": "EC2 만들어줘"})["ok"]
    assert wait_for(lambda: box.svc.view(sid, "alice")["pipeline"]["status"] == "manual" and len(box.svc.view(sid, "alice")["thread"]) >= 3)
    _join_generation_threads()
    assert box.svc.handle(sid, "alice", {"type": "answer", "text": "t3.micro로"})["ok"]                              # 되묻기 다음의 답: 앞 내용과 이어서 보낸다
    assert wait_for(lambda: any("초안" in m["text"] for m in box.svc.view(sid, "alice")["thread"]))
    _join_generation_threads()
    assert "[이전 질문]" in box.pipeline.seen[-1] and "EC2 만들어줘" in box.pipeline.seen[-1] and "t3.micro로" in box.pipeline.seen[-1]
    box.store.close(); box.maps.close()


def test_an_embedding_failure_is_explained_and_does_not_lock_an_empty_plan(empty):
    from app.rag.embedder import EmbeddingError

    class Broken:
        def run(self, *a, **k):
            raise EmbeddingError("AccessDeniedException")
    box, sid = empty
    box.svc._pipeline_factory = lambda masker, s: Broken()
    assert box.svc.handle(sid, "alice", {"type": "answer", "text": "EC2 만들어줘"})["ok"]
    assert wait_for(lambda: any("임베딩" in m["text"] for m in box.svc.view(sid, "alice")["thread"]))
    _join_generation_threads()
    v = box.svc.view(sid, "alice")
    assert v["pipeline"]["status"] == "manual" and v["pipeline"]["reasons"] == ["EmbeddingError"]                    # 빈 plan은 계속 쓸 수 있다
    assert S(box, sid, "alice", op="add_step", step={"kind": "cli", "title": "직접", "command": "aws ec2 describe-vpcs"})["ok"]


def test_human_steps_like_iac_changes_can_be_drafted_confirmed_and_sent_without_a_command(empty):
    box, sid = empty
    r = S(box, sid, "alice", op="add_step", step={"kind": "iac", "title": "SG 코드 변경", "checklist": ["Terraform 코드 변경 PR", "terraform plan 확인"]})
    assert r["ok"]
    st = steps(box, sid)[r["added"][0]]
    assert st["status"] == "ok" and st["issues"] == [] and st["command"] is None                      # 명령 없이 확정할 수 있다
    assert S(box, sid, "alice", op="set_confirmed", step_ids=[st["id"]], value=True)["ok"]
    assert box.svc.handle(sid, "alice", {"type": "send_to_apply"})["ok"]
    v = box.svc.view(sid, "alice")
    sent = next(x for p in v["plan"]["phases"] for x in p["steps"] if x["id"] == st["id"])
    assert sent["kind"] == "iac" and sent["checklist"] == ["Terraform 코드 변경 PR", "terraform plan 확인"]


def test_a_human_step_without_anything_to_do_is_flagged_and_the_kind_can_be_changed(empty):
    box, sid = empty
    r = S(box, sid, "alice", op="add_step", step={"kind": "check", "title": "확인"})
    sid_ = r["added"][0]
    assert {i["code"] for i in steps(box, sid)[sid_]["issues"]} == {"checklist_required"}
    assert S(box, sid, "alice", op="set_confirmed", step_ids=[sid_], value=True)["ok"] is False
    assert S(box, sid, "alice", op="update_step", step_id=sid_, fields={"checklist": ["확인할 것"]})["ok"]
    assert steps(box, sid)[sid_]["status"] == "ok"
    assert S(box, sid, "alice", op="update_step", step_id=sid_, fields={"kind": "cli"})["ok"]
    assert "command_required" in {i["code"] for i in steps(box, sid)[sid_]["issues"]}                 # 종류를 바꾸면 그 종류의 규칙으로 다시 본다


# ---------------- 앞 절차 결과 참조(D-48) ----------------
def _mk_and_rule(box, sid):
    S(box, sid, "alice", op="add_step", step={"id": "mk-sg", "kind": "cli", "title": "SG 생성", "command": "aws ec2 create-security-group --group-name web --description web"})
    S(box, sid, "alice", op="add_step", step={"id": "mk-chk", "kind": "cli", "title": "SG 확인", "command": "aws ec2 describe-security-groups --group-ids @{mk-sg.GroupId}", "check_for": "mk-sg"})
    cidr = box.svc.masker_for(sid).mask("10.20.0.0/16")
    S(box, sid, "alice", op="add_step", step={"id": "rule", "kind": "cli", "title": "규칙 추가", "command": f"aws ec2 authorize-security-group-ingress --group-id @{{mk-sg.GroupId}} --protocol tcp --port 443 --cidr {cidr}"})
    S(box, sid, "alice", op="add_step", step={"id": "rule-chk", "kind": "cli", "title": "규칙 확인", "command": "aws ec2 describe-security-groups --group-ids @{mk-sg.GroupId}", "check_for": "rule"})


def test_a_later_step_can_use_an_earlier_steps_resource_id_and_the_pad_offers_the_field(empty):
    box, sid = empty
    _mk_and_rule(box, sid)
    st = steps(box, sid)
    assert st["rule"]["status"] == "ok" and st["rule"]["uses"] == ["mk-sg"]
    assert st["mk-sg"]["ref_fields"] == [{"path": "GroupId", "label": "보안 그룹 ID"}]               # 화면의 "앞 절차 결과 쓰기" 목록
    assert "uses_output" in st["rule"]["flags"]
    r = S(box, sid, "alice", op="set_confirmed", step_ids=["rule-chk"], value=True)
    assert r["ok"] and set(r["confirmed"]) == {"mk-sg", "mk-chk", "rule", "rule-chk"}                 # 참조한 절차까지 묶여 확정된다
    assert box.svc.handle(sid, "alice", {"type": "send_to_apply"})["ok"]
    plan = {x["id"]: x for p in box.svc.view(sid, "alice")["plan"]["phases"] for x in p["steps"]}
    assert "@{mk-sg.GroupId}" in plan["rule"]["command"] and "mk-sg" in plan["rule"]["depends_on"]    # 템플릿 그대로, 의존 자동 추가
    assert S(box, sid, "alice", op="set_confirmed", step_ids=["mk-sg"], value=False)["ok"]            # 참조된 절차의 확정을 풀면 참조한 절차도 풀린다
    assert not any(x["confirmed"] for x in steps(box, sid).values())


def test_a_reference_to_non_json_output_is_flagged_in_the_pad(empty):
    box, sid = empty
    S(box, sid, "alice", op="add_step", step={"id": "get", "kind": "cli", "title": "조회", "command": "aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text"})
    S(box, sid, "alice", op="add_step", step={"id": "use", "kind": "cli", "title": "사용", "command": "aws ec2 describe-images --image-ids @{get.Parameter.Value}"})
    assert "ref_target_not_json" in {x["code"] for x in steps(box, sid)["use"]["issues"]} and steps(box, sid)["use"]["status"] == "warn"


def test_bad_references_are_flagged_in_the_pad(empty):
    box, sid = empty
    S(box, sid, "alice", op="add_step", step={"id": "late", "kind": "cli", "title": "먼저", "command": "aws ec2 describe-security-groups --group-ids @{mk-sg.GroupId}"})
    S(box, sid, "alice", op="add_step", step={"id": "mk-sg", "kind": "cli", "title": "SG 생성", "command": "aws ec2 create-security-group --group-name web --description web"})
    S(box, sid, "alice", op="add_step", step={"id": "ghost", "kind": "cli", "title": "없는 절차", "command": "aws ec2 describe-security-groups --group-ids @{nope.GroupId}"})
    S(box, sid, "alice", op="add_step", step={"id": "hum", "kind": "iac", "title": "코드", "checklist": ["x"]})
    S(box, sid, "alice", op="add_step", step={"id": "onhum", "kind": "cli", "title": "사람 절차 참조", "command": "aws ec2 describe-security-groups --group-ids @{hum.GroupId}"})
    S(box, sid, "alice", op="add_step", step={"id": "bad", "kind": "cli", "title": "문법", "command": "aws ec2 describe-security-groups --group-ids @{mk-sg.GroupId; id}"})
    code = lambda i: {x["code"] for x in steps(box, sid)[i]["issues"]}                                    # noqa: E731
    assert "ref_order" in code("late") and "ref_unknown_step" in code("ghost") and "ref_target_not_runnable" in code("onhum")
    assert "blocked" in code("bad") and steps(box, sid)["bad"]["status"] == "err"


def test_the_agent_is_told_to_draft_with_defaults_instead_of_asking_only_in_drafts_mode(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, statuses=["plan", "plan"])
    c = box.svc.create("alice", "빈 작업")                                                             # 빈 plan: 초안 모드
    box.svc.generate_plan(c["session_id"], "default VPC에 EC2 하나 만들어줘")
    assert box.pipeline.drafts is True
    n = box.svc.create("alice", "일반 작업", "default VPC에 EC2 하나 만들어줘")                           # 요청으로 시작한 세션: plan으로 제출
    box.svc.generate_plan(n["session_id"], "default VPC에 EC2 하나 만들어줘")
    assert box.pipeline.drafts is True                                                                 # 일반 요청 세션도 에이전트는 되묻지 않고 기본값으로 만든다(결과는 plan으로 제출)
    assert box.svc.view(n["session_id"], "alice")["session"]["state"] == "plan_review"
    box.store.close(); box.maps.close()


def test_a_request_session_switches_to_drafts_mode_once_the_pad_has_steps(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, statuses=["clarify", "plan"])
    c = box.svc.create("alice", "되묻는 작업", REQUEST)
    sid = c["session_id"]
    box.svc.generate_plan(sid, REQUEST)
    assert box.pipeline.drafts is True and box.svc.view(sid, "alice")["pipeline"]["status"] == "clarify"
    S(box, sid, "alice", op="add_step", step={"kind": "cli", "title": "내 절차", "command": "aws ec2 describe-vpcs"})
    box.svc.generate_plan(sid, REQUEST)
    assert box.pipeline.drafts is True and box.svc.view(sid, "alice")["pipeline"]["status"] == "manual"   # 연습장에 절차가 있으면 결과는 초안으로만 덧붙는다
    box.store.close(); box.maps.close()
