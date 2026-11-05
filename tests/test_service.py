"""세션 서비스: 생성·계획 연결·동작 검증·화면 스냅샷의 복원·재시작 후 이어가기·종료 시 매핑 삭제·poll 스레드."""
import time

import pytest
from test_apply import FIXTURE, INSTANCE, ScriptedExecutor, chunks, snapshot  # noqa: F401  (픽스처 재사용)

from app.guards.cli_snapshot import CliSnapshot  # noqa: F401
from app.guards.command_guard import CommandGuard, CommandIndex
from app.guards.mapping_store import SqliteMappingStore
from app.guards.masking import Inventory, Masker
from app.planner.agent import PlanResult
from app.planner.pipeline import PipelineResult
from app.planner.review import ReviewFacts, ReviewResult
from app.planner.schema import PlanDraft, build_plan
from app.session.service import ServiceError, SessionService
from app.session.store import SessionStore

REQUEST = f"{INSTANCE} 를 재부팅해줘"


class FakePipeline:
    def __init__(self, plan, status="plan"):
        self._plan, self._status = plan, status
        self.seen = []

    def run(self, text, *, iac_managed=False, session_id=None):
        self.seen.append(text)
        if self._status != "plan":
            return PipelineResult(self._status, message="되묻기", questions=({"param": "대상", "question": "어느 것?"},))
        return PipelineResult("plan", plan_result=PlanResult("plan", plan=self._plan),
                              review=ReviewResult(ReviewFacts("medium", ()), summary="요약"), evidence={"reboot": {"grounded": True}})


def make_plan(guard, masker, extra=()):
    ph = masker.mask(INSTANCE)
    steps = [{"id": "reboot", "kind": "cli", "title": "재부팅", "command": f"aws ec2 reboot-instances --instance-ids {ph}"},
             {"id": "verify", "kind": "cli", "title": "확인", "command": f"aws ec2 describe-instance-status --instance-ids {ph}", "check_for": "reboot"}, *extra]
    d = PlanDraft.model_validate({"summary": "t", "phases": [{"id": "p1", "title": "phase", "steps": steps}]})
    return build_plan(d, guard, masker, postprocess=False)


class Env:
    pass


def build(tmp_path, chunks, snapshot, *, extra=(), pipeline_status="plan", statuses=None, sleep=lambda s: time.sleep(0.01), analyzer_factory=None):
    guard = CommandGuard(CommandIndex(chunks), snapshot, workspace=tmp_path)
    store = SessionStore(tmp_path / "s.sqlite")
    maps = SqliteMappingStore(tmp_path / "m.sqlite")
    ex = ScriptedExecutor(guard)
    box = Env()
    box.guard, box.store, box.maps, box.ex, box.extra = guard, store, maps, ex, extra
    box.pipelines = []

    def factory(masker, sid):
        box.pipeline = FakePipeline(make_plan(guard, masker, extra), statuses.pop(0) if statuses else pipeline_status)
        box.pipelines.append(box.pipeline)
        return box.pipeline

    box.svc = SessionService(store, maps, executor=ex, guard=guard, pipeline_factory=factory,
                             inventory_factory=lambda: Inventory.from_file(FIXTURE), sleep=sleep, analyzer_factory=analyzer_factory)
    return box


@pytest.fixture()
def b(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot)
    yield box
    box.store.close()
    box.maps.close()


def started(b, text=REQUEST):
    c = b.svc.create("alice", "테스트 작업", text)
    sid = c["session_id"]
    b.svc.generate_plan(sid, text)
    j = b.svc.join(sid, c["invite"], "bob")
    return sid, c, j


# ---------------- 생성 ----------------
def test_input_guard_rejection_creates_no_session(b):
    with pytest.raises(ServiceError) as e:
        b.svc.create("alice", "테스트 작업", "password=hunter2 로 사용자 만들어")
    assert e.value.code == "input_rejected" and "hunter2" not in e.value.message
    assert b.store.sessions_in("draft") == []


def test_stored_request_is_masked_and_tokens_authenticate(b):
    c = b.svc.create("alice", "테스트 작업", REQUEST)
    s = b.store.session(c["session_id"])
    assert INSTANCE not in s["masked_request"] and "<INSTANCE_1>" in s["masked_request"]
    assert b.svc.authenticate(c["session_id"], c["member_token"]) == "alice" and b.svc.authenticate(c["session_id"], "x") is None
    with pytest.raises(ServiceError, match="invalid_nickname"):
        b.svc.create("a b", "테스트 작업", REQUEST)


def test_pipeline_result_other_than_plan_leaves_the_session_in_draft(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, pipeline_status="clarify")
    c = box.svc.create("alice", "테스트 작업", REQUEST)
    box.svc.generate_plan(c["session_id"], REQUEST)
    v = box.svc.view(c["session_id"], "alice")
    assert v["session"]["state"] == "draft" and v["pipeline"]["status"] == "clarify" and v["pipeline"]["questions"] and v["version"] is None


# ---------------- 흐름과 스냅샷 ----------------
def test_full_flow_through_handle_with_real_values_only_in_the_view(b, tmp_path):
    sid, c, _ = started(b)
    assert b.store.session(sid)["state"] == "plan_review"
    view = b.svc.view(sid, "alice")
    assert view["review"]["summary"] == "요약" and view["evidence"]["reboot"]["grounded"]
    assert INSTANCE in str(view["plan"]) and view["you"] == {"nick": "alice", "role": "requester"}        # 화면에는 실제 값
    h = view["plan_hash"]
    assert b.svc.handle(sid, "alice", {"type": "approve_plan", "version": 1, "hash": h}) == {"ok": False, "code": "requester_cannot_approve_plan"}
    assert b.svc.handle(sid, "bob", {"type": "approve_plan", "version": 1, "hash": h})["ok"]
    assert b.svc.handle(sid, "alice", {"type": "start_apply"})["ok"]
    assert b.svc.handle(sid, "bob", {"type": "approve_step", "version": 1, "step": "reboot", "hash": h})["ok"]
    assert b.svc.handle(sid, "bob", {"type": "run_step", "version": 1, "step": "reboot"})["ok"]
    v2 = b.svc.view(sid, "bob")
    assert v2["session"]["state"] == "completed" and v2["steps"]["verify"]["state"] == "done"
    assert INSTANCE in b.ex.ran[0].command                                              # 실행기는 실제 값
    for db in ("s.sqlite", "m.sqlite"):                                                 # 저장된 어디에도 실제 값이 평문으로 없다
        assert INSTANCE.encode() not in (tmp_path / db).read_bytes()


def test_malformed_messages_are_refused_with_codes(b):
    sid, c, _ = started(b)
    assert b.svc.handle(sid, "bob", {"type": "nope"}) == {"ok": False, "code": "unknown_message"}
    assert b.svc.handle(sid, "bob", {}) == {"ok": False, "code": "missing_field:type"}
    assert b.svc.handle(sid, "bob", {"type": "approve_plan", "version": "1", "hash": "x"})["code"] == "bad_field:version"
    assert b.svc.handle(sid, "bob", {"type": "approve_plan", "version": True, "hash": "x"})["code"] == "bad_field:version"
    assert b.svc.handle(sid, "bob", {"type": "approve_plan", "version": 1})["code"] == "missing_field:hash"
    assert b.svc.handle(sid, "mallory", {"type": "approve_plan", "version": 1, "hash": "x"})["code"] == "not_a_participant"
    assert b.svc.handle(sid, "bob", {"type": "edit", "op": "drop_table"})["code"] == "unknown_edit_op"


def test_view_shows_pending_requirements_and_review_for_participants(b):
    sid, c, _ = started(b)
    v = b.svc.view(sid, "bob")
    assert v["you"]["role"] == "approver" and v["plan_status"] == "draft" and v["steps"]["reboot"]["state"] == "pending"


# ---------------- 수정 ----------------
def test_edit_creates_a_new_version_in_review_but_is_blocked_while_applying(b):
    sid, c, _ = started(b)
    r = b.svc.handle(sid, "bob", {"type": "edit", "op": "add_step", "phase": "p1",
                                  "step": {"id": "mine", "kind": "cli", "title": "조회", "command": "aws ec2 describe-vpcs"}})
    assert r["ok"] and b.store.latest_version(sid) == 2
    v = b.svc.view(sid, "alice")
    b.svc.handle(sid, "bob", {"type": "approve_plan", "version": 2, "hash": v["plan_hash"]})
    b.svc.handle(sid, "alice", {"type": "start_apply"})
    r = b.svc.handle(sid, "alice", {"type": "edit", "op": "delete_step", "step_id": "mine"})
    assert r == {"ok": False, "code": "plan_edit_not_allowed:applying"}
    assert b.svc.handle(sid, "alice", {"type": "pause"})["ok"]
    assert b.svc.handle(sid, "alice", {"type": "edit", "op": "delete_step", "step_id": "mine"})["ok"]      # 일시정지 후에는 부분 재계획
    assert b.store.session(sid)["state"] == "plan_review" and b.store.latest_version(sid) == 3


def test_invalid_edit_returns_plan_codes_not_values(b):
    sid, c, _ = started(b)
    r = b.svc.handle(sid, "bob", {"type": "edit", "op": "add_step", "phase": "p1",
                                  "step": {"id": "x", "kind": "cli", "title": "t", "command": "aws ec2 describe-vpcs --profile prod"}})
    assert r["ok"] is False and r["code"] == "plan_invalid" and any(c.startswith("blocked_command") for c in r["codes"])


# ---------------- 재시작·매핑 ----------------
def test_after_a_restart_the_session_continues_and_the_view_still_shows_real_values(tmp_path, chunks, snapshot):
    a = build(tmp_path, chunks, snapshot)
    sid, c, _ = started(a)
    h = a.svc.view(sid, "alice")["plan_hash"]
    a.svc.handle(sid, "bob", {"type": "approve_plan", "version": 1, "hash": h})
    a.store.close(); a.maps.close()
    b2 = build(tmp_path, chunks, snapshot)                                         # 같은 DB로 새 서비스(서버 재시작)
    assert b2.svc.authenticate(sid, c["member_token"]) == "alice"
    v = b2.svc.view(sid, "alice")
    assert INSTANCE in str(v["plan"]) and v["session"]["state"] == "plan_approved"
    assert b2.svc.handle(sid, "alice", {"type": "start_apply"})["ok"]
    b2.store.close(); b2.maps.close()


def test_mapping_is_deleted_when_the_session_ends(b, tmp_path):
    sid, c, _ = started(b)
    h = b.svc.view(sid, "alice")["plan_hash"]
    b.svc.handle(sid, "bob", {"type": "approve_plan", "version": 1, "hash": h})
    b.svc.handle(sid, "alice", {"type": "start_apply"})
    probe = Masker(Inventory.empty())
    assert b.maps.load_into(sid, probe) is True
    assert b.svc.handle(sid, "alice", {"type": "abort", "version": 1})["ok"]
    assert b.maps.load_into(sid, Masker(Inventory.empty())) is False               # 종료·취소되면 매핑이 지워진다


def test_recover_all_interrupts_running_steps_after_a_restart(b):
    sid, c, _ = started(b)
    h = b.svc.view(sid, "alice")["plan_hash"]
    b.svc.handle(sid, "bob", {"type": "approve_plan", "version": 1, "hash": h})
    b.svc.handle(sid, "alice", {"type": "start_apply"})
    b.svc.handle(sid, "bob", {"type": "approve_step", "version": 1, "step": "reboot", "hash": h})
    b.store.transition_step(sid, 1, "reboot", "running")                           # 실행 중 서버가 죽었다고 가정
    assert b.svc.recover_all() == {sid: {"resumable": [], "interrupted": ["reboot"]}}
    assert b.store.session(sid)["state"] == "paused"


# ---------------- poll 스레드 ----------------
def test_poll_runs_in_the_background_until_a_person_confirms(tmp_path, chunks, snapshot):
    poll = ({"id": "wait", "kind": "poll", "title": "대기", "command": "aws ec2 describe-instance-status --instance-ids <INSTANCE_1>",
             "poll": {"interval_seconds": 5, "max_seconds": 600, "until": "ok"}},)
    box = build(tmp_path, chunks, snapshot, extra=poll)
    sid, c, _ = started(box)
    h = box.svc.view(sid, "alice")["plan_hash"]
    box.svc.handle(sid, "bob", {"type": "approve_plan", "version": 1, "hash": h})
    box.svc.handle(sid, "alice", {"type": "start_apply"})
    assert box.svc.handle(sid, "alice", {"type": "approve_step", "version": 1, "step": "wait", "hash": h})["ok"]
    assert box.svc.handle(sid, "alice", {"type": "start_poll", "version": 1, "step": "wait"})["ok"]
    for _ in range(300):
        if len([o for o in box.store.outputs(sid, 1, "wait") if o["status"].startswith("poll")]) >= 3:
            break
        time.sleep(0.02)
    assert box.svc.handle(sid, "bob", {"type": "poll_complete", "version": 1, "step": "wait"})["ok"]
    for _ in range(100):
        if not box.svc._threads[(sid, "wait")].is_alive():
            break
        time.sleep(0.02)
    assert box.store.step_states(sid, 1)["wait"]["state"] == "done"
    box.store.close(); box.maps.close()


def test_listeners_are_notified_after_actions_and_errors_in_them_are_ignored(b):
    seen = []
    b.svc.listeners += [lambda sid: seen.append(sid), lambda sid: 1 / 0]
    c = b.svc.create("alice", "테스트 작업", REQUEST)
    b.svc.handle(c["session_id"], "alice", {"type": "nope"})
    assert seen == [c["session_id"]]


# ---------------- 같은 세션에서 추가 정보를 이어서 받는다 ----------------
def wait_for(cond, tries=300):
    for _ in range(tries):
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_a_clarification_is_answered_in_the_same_session_and_the_plan_follows(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, statuses=["clarify", "plan"])
    c = box.svc.create("alice", "테스트 작업", REQUEST)
    sid = c["session_id"]
    assert box.svc.view(sid, "alice")["pipeline"]["status"] == "generating"            # 생성 중 표시
    box.svc.generate_plan(sid, REQUEST)
    v = box.svc.view(sid, "alice")
    assert v["pipeline"]["status"] == "clarify" and [m["role"] for m in v["thread"]] == ["user", "assistant"] and "어느 것?" in v["thread"][1]["text"]
    assert box.svc.handle(sid, "alice", {"type": "answer", "text": f"{INSTANCE} 하나만이고 타입은 t3.micro"})["ok"]
    assert wait_for(lambda: box.svc.view(sid, "alice")["pipeline"]["status"] == "plan")
    seen = box.pipelines[-1].seen[0]
    assert "[추가 정보]" in seen and "t3.micro" in seen and INSTANCE not in seen                 # 모델 쪽 입력은 마스킹된 요청 + 보탠 내용
    v = box.svc.view(sid, "alice")
    assert v["session"]["state"] == "plan_review" and [m["role"] for m in v["thread"]] == ["user", "assistant", "user", "assistant"]
    assert INSTANCE in v["thread"][2]["text"]                                                   # 화면에는 실제 값
    box.store.close(); box.maps.close()


def test_answers_are_limited_to_the_requester_the_right_moment_and_the_input_guard(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, statuses=["clarify", "plan"])
    c = box.svc.create("alice", "테스트 작업", REQUEST)
    sid = c["session_id"]
    box.svc.join(sid, c["invite"], "bob")
    assert box.svc.handle(sid, "alice", {"type": "answer", "text": "아직 생성 중"})["code"] == "answer_not_expected"        # 생성 중에는 받지 않는다
    box.svc.generate_plan(sid, REQUEST)
    assert box.svc.handle(sid, "bob", {"type": "answer", "text": "내가 대신"})["code"] == "answer_by_requester_only"
    r = box.svc.handle(sid, "alice", {"type": "answer", "text": "password=hunter2"})
    assert r["ok"] is False and r["code"] == "input_rejected" and "hunter2" not in str(r)
    assert box.svc.handle(sid, "alice", {"type": "answer", "text": "t3.micro 로"})["ok"]
    assert wait_for(lambda: box.svc.view(sid, "alice")["pipeline"]["status"] == "plan")
    assert box.svc.handle(sid, "alice", {"type": "answer", "text": "더 보태기"})["code"] == "answer_not_expected"          # 계획이 만들어진 뒤에는 받지 않는다
    box.store.close(); box.maps.close()


def test_the_conversation_has_a_length_limit_and_a_restart_unsticks_a_generating_session(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, pipeline_status="clarify")
    c = box.svc.create("alice", "테스트 작업", REQUEST)
    sid = c["session_id"]
    box.svc.generate_plan(sid, REQUEST)
    last = None
    for i in range(8):
        last = box.svc.handle(sid, "alice", {"type": "answer", "text": f"보충 {i}"})
        if not last["ok"]:
            break
        assert wait_for(lambda: box.svc.view(sid, "alice")["pipeline"]["status"] == "clarify")
    assert last["code"] == "conversation_too_long"
    c2 = box.svc.create("alice", "테스트 작업", REQUEST)                                                        # 생성 중(generating)인 채 서버가 멈춘 세션
    assert box.svc.view(c2["session_id"], "alice")["pipeline"]["status"] == "generating"
    box.svc.recover_all()
    v = box.svc.view(c2["session_id"], "alice")
    assert v["pipeline"]["status"] == "stopped" and "다시 보내" in v["thread"][-1]["text"]
    assert box.svc.handle(c2["session_id"], "alice", {"type": "answer", "text": "다시 시도"})["ok"]                # 멈춘 세션은 이어서 보낼 수 있다
    box.store.close(); box.maps.close()


# ---------------- 제목, 이력(DB), 초대 링크 재발급 ----------------
def test_the_title_is_required_masked_and_screened(b):
    for bad in ("", "   ", "x" * 61):
        with pytest.raises(ServiceError) as e:
            b.svc.create("alice", bad, REQUEST)
        assert e.value.code == "title_required"
    with pytest.raises(ServiceError) as e:
        b.svc.create("alice", "password=hunter2 작업", REQUEST)
    assert e.value.code == "input_rejected" and "hunter2" not in e.value.message
    c = b.svc.create("alice", f"{INSTANCE} 재부팅", REQUEST)
    assert INSTANCE not in b.store.session(c["session_id"])["title"]                       # DB에는 마스킹된 제목
    assert b.svc.view(c["session_id"], "alice")["session"]["title"] == f"{INSTANCE} 재부팅"   # 화면에는 실제 값


def test_history_comes_from_the_database_for_the_tokens_the_browser_holds(b):
    c1 = b.svc.create("alice", "첫 작업", REQUEST)
    c2 = b.svc.create("alice", "두번째 작업", REQUEST)
    b.svc.generate_plan(c1["session_id"], REQUEST)
    other = b.svc.create("carol", "남의 작업", REQUEST)
    items = b.svc.history([{"sid": c1["session_id"], "token": c1["member_token"]}, {"sid": c2["session_id"], "token": c2["member_token"]},
                           {"sid": other["session_id"], "token": c1["member_token"]},            # 남의 세션에 내 토큰: 내용을 주지 않는다
                           {"sid": "nope", "token": "x"}])
    valid = [i for i in items if i["valid"]]
    assert [i["title"] for i in valid] == ["두번째 작업", "첫 작업"] or {i["title"] for i in valid} == {"첫 작업", "두번째 작업"}
    first = next(i for i in valid if i["title"] == "첫 작업")
    assert first["state"] == "plan_review" and first["steps"] == {"pending": 2} and first["risk"] == "medium" and first["nick"] == "alice"
    assert first["participants"] == 1                                                              # 작업자 수(이력의 상태 점이 초대 완료 여부를 구분한다)
    b.svc.join(c1["session_id"], c1["invite"], "bob")
    again = {i["sid"]: i for i in b.svc.history([{"sid": c1["session_id"], "token": c1["member_token"]}])}
    assert again[c1["session_id"]]["participants"] == 2
    assert [i for i in items if not i["valid"]] and all("title" not in i for i in items if not i["valid"])
    assert "남의 작업" not in str(items)


def test_the_requester_can_create_a_fresh_invite_and_the_old_link_stops_working(b):
    c = b.svc.create("alice", "초대 시험", REQUEST)
    sid = c["session_id"]
    b.svc.join(sid, c["invite"], "bob")
    r = b.svc.handle(sid, "alice", {"type": "create_invite"})
    assert r["ok"] and r["invite"] and r["invite"] != c["invite"]
    with pytest.raises(ServiceError, match="invalid_invite"):
        b.svc.join(sid, c["invite"], "carol")                                              # 이전 링크는 무효
    assert b.svc.join(sid, r["invite"], "carol")["member_token"]
    assert b.svc.handle(sid, "bob", {"type": "create_invite"}) == {"ok": False, "code": "invite_by_requester_only"}
    assert r["invite"].encode() not in b.store._db.execute("SELECT group_concat(invite_hash) FROM sessions").fetchone()[0].encode()      # 해시만 저장


def test_a_change_and_its_check_can_be_added_together(b):
    sid, c, _ = started(b)
    ph = "<INSTANCE_1>"
    r = b.svc.handle(sid, "bob", {"type": "edit", "op": "add_steps", "phase": "p1", "steps": [
        {"id": "stop", "kind": "cli", "title": "중지", "command": f"aws ec2 stop-instances --instance-ids {ph}"},
        {"id": "stop-chk", "kind": "cli", "title": "확인", "check_for": "stop", "command": f"aws ec2 describe-instance-status --instance-ids {ph}"}]})
    assert r["ok"] and b.store.latest_version(sid) == 2
    r = b.svc.handle(sid, "bob", {"type": "edit", "op": "add_steps", "phase": "p1", "steps": [
        {"id": "start", "kind": "cli", "title": "시작", "command": f"aws ec2 start-instances --instance-ids {ph}"}]})
    assert r["ok"] is False and "missing_check:start" in r["codes"]                         # 짝이 없으면 서버가 거부


def test_a_step_can_be_moved_to_the_front_and_null_optional_fields_are_ignored(tmp_path, chunks, snapshot):
    """브라우저 확인에서 발견: 화면이 맨 앞으로 옮길 때 보내는 after: null을 서버가 거부했다."""
    box = build(tmp_path, chunks, snapshot, extra=({"id": "peek", "kind": "cli", "title": "조회", "command": "aws ec2 describe-vpcs"},))
    c = box.svc.create("alice", "정렬", REQUEST)
    sid = c["session_id"]
    box.svc.generate_plan(sid, REQUEST)

    def rows():
        return [s["id"] for ph in box.svc.view(sid, "alice")["plan"]["phases"] for s in ph["steps"]]
    assert rows() == ["reboot", "verify", "peek"]
    assert box.svc.handle(sid, "alice", {"type": "edit", "op": "move_step", "step_id": "peek", "after": None})["ok"]
    r1 = rows()                                                                                # 편집하면 서버가 사전 점검(pre-reboot)을 넣을 수 있다
    assert r1.index("peek") < r1.index("reboot") < r1.index("verify") and r1[0] in ("peek", "pre-reboot")
    assert box.svc.handle(sid, "alice", {"type": "edit", "op": "move_step", "step_id": "peek", "after": "reboot"})["ok"]
    r2 = rows()
    assert r2.index("reboot") < r2.index("peek")
    r = box.svc.handle(sid, "alice", {"type": "edit", "op": "move_step", "step_id": "peek", "after": 5})
    assert r == {"ok": False, "code": "bad_field:after"}                                      # null이 아닌 잘못된 형식은 여전히 거부
    box.store.close(); box.maps.close()


# ---------------- 중단 안내와 다시 시도 ----------------
def test_stop_messages_explain_the_reason_without_values():
    from app.planner.pipeline import stop_message
    m = stop_message("repeated_call", ("search_docs",))
    assert "같은 조회를 반복" in m and "search_docs" in m and "다시 시도" in m
    u = stop_message("model_output_unsafe", ("resource_id", "ip"))
    assert "리소스 ID" in u and "IP/CIDR" in u and "민감값" in u
    assert "한도" in stop_message("tool_calls_exceeded") and "모델을 지금 사용할 수 없" in stop_message("llm_unavailable")
    assert "다시 시도" in stop_message("something_new")                                          # 모르는 코드도 일반 안내


def test_a_stopped_generation_can_be_retried_with_the_same_content(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, statuses=["stopped", "plan"])
    c = box.svc.create("alice", "재시도 작업", REQUEST)
    sid = c["session_id"]
    box.svc.join(sid, c["invite"], "bob")
    assert box.svc.handle(sid, "alice", {"type": "retry_generation"})["code"] == "retry_not_expected"        # 생성 중에는 받지 않는다
    box.svc.generate_plan(sid, REQUEST)
    assert box.svc.view(sid, "alice")["pipeline"]["status"] == "stopped"
    assert box.svc.handle(sid, "bob", {"type": "retry_generation"})["code"] == "retry_by_requester_only"
    assert box.svc.handle(sid, "alice", {"type": "retry_generation"})["ok"]
    assert wait_for(lambda: box.svc.view(sid, "alice")["pipeline"]["status"] == "plan")
    sent = box.pipelines[-1].seen[0]
    assert "다시 시도" not in sent and sent.count("[추가 정보]") == 0                                   # 같은 요청이 그대로 가고, 표시 문구는 모델로 가지 않는다
    assert box.svc.handle(sid, "alice", {"type": "retry_generation"})["code"] == "retry_not_expected"        # 계획이 만들어진 뒤에는 받지 않는다
    box.store.close(); box.maps.close()


# ---------------- 읽기 절차는 확인 한 번으로, 실패는 대화로 ----------------
def started_apply(box, text=REQUEST):
    c = box.svc.create("alice", "적용 시험", text)
    sid = c["session_id"]
    box.svc.generate_plan(sid, text)
    box.svc.join(sid, c["invite"], "bob")
    h = box.svc.view(sid, "alice")["plan_hash"]
    box.svc.handle(sid, "bob", {"type": "approve_plan", "version": 1, "hash": h})
    box.svc.handle(sid, "alice", {"type": "start_apply"})
    return sid, h


def test_read_steps_are_confirmed_with_one_click_but_changes_keep_the_approval_flow(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, extra=({"id": "peek", "kind": "cli", "title": "조회", "command": "aws ec2 describe-vpcs"},))
    sid, h = started_apply(box)
    msg = {"type": "run_read", "version": 1, "step": "peek", "hash": h}
    assert box.svc.handle(sid, "alice", {**msg, "hash": "0" * 64})["code"] == "plan_hash_mismatch"
    assert box.svc.handle(sid, "alice", msg)["ok"]                                       # 요청자도 승인 없이 확인으로 실행
    assert box.store.step_states(sid, 1)["peek"]["state"] == "done" and [r.step_id for r in box.ex.ran] == ["peek"]
    assert box.svc.handle(sid, "alice", {"type": "run_read", "version": 1, "step": "reboot", "hash": h})["code"] == "confirm_not_allowed"      # 변경은 승인 절차
    assert [r.step_id for r in box.ex.ran] == ["peek"]
    assert box.svc.handle(sid, "alice", {"type": "run_read", "version": 1, "step": "verify", "hash": h})["code"] == "dependency_not_done:reboot"      # 순서·의존 규칙은 그대로
    box.store.close(); box.maps.close()


def test_a_failure_is_explained_in_the_chat_and_shows_a_generating_indicator_while_analyzing(tmp_path, chunks, snapshot):
    from app.planner.failure import FailureAnalysis
    seen = {}

    class Analyzer:
        def analyze(self, plan, step_id, result, *, attempt, session_id):
            seen["during"] = box.svc.view(session_id, "alice")["analyzing"]           # 분석하는 동안의 화면 상태
            seen["thread_during"] = [m["text"] for m in box.svc.view(session_id, "alice")["thread"]][-1]
            return FailureAnalysis("analysis", step_id=step_id, attempt=attempt, server_hint="throttled", cause="호출이 몰려 요청이 제한됨", category="throttled",
                                   recommendation="retry", needs_state_check=False)

    box = build(tmp_path, chunks, snapshot, analyzer_factory=lambda masker: Analyzer())
    sid, h = started_apply(box)
    box.ex.script = [("failed", "", "An error occurred (ThrottlingException)")]
    box.svc.handle(sid, "bob", {"type": "approve_step", "version": 1, "step": "reboot", "hash": h})
    box.svc.handle(sid, "bob", {"type": "run_step", "version": 1, "step": "reboot"})
    assert seen["during"] == "reboot" and "분석하는 중" in seen["thread_during"]
    v = box.svc.view(sid, "alice")
    assert v["analyzing"] is None
    texts = [m["text"] for m in v["thread"] if m["role"] == "assistant"]
    assert any("절차 reboot가 실패했습니다(요청 제한)" in t for t in texts)
    assert any("실패 분석(AI 추정, 참고용): 호출이 몰려 요청이 제한됨" in t and "추천: 재시도" in t for t in texts)
    box.store.close(); box.maps.close()


def test_a_failure_without_an_analysis_still_tells_the_user_what_to_do(tmp_path, chunks, snapshot):
    class Broken:
        def analyze(self, *a, **k):
            raise RuntimeError("model down")

    box = build(tmp_path, chunks, snapshot, analyzer_factory=lambda masker: Broken())
    sid, h = started_apply(box)
    box.ex.script = [("failed", "", "An error occurred (AccessDenied)")]
    box.svc.handle(sid, "bob", {"type": "approve_step", "version": 1, "step": "reboot", "hash": h})
    box.svc.handle(sid, "bob", {"type": "run_step", "version": 1, "step": "reboot"})
    v = box.svc.view(sid, "alice")
    texts = [m["text"] for m in v["thread"] if m["role"] == "assistant"]
    assert v["analyzing"] is None and any("권한 오류" in t for t in texts) and any("분석하지 못했습니다" in t for t in texts)
    box.store.close(); box.maps.close()

    box2 = build(tmp_path / "n", chunks, snapshot) if (tmp_path / "n").mkdir() is None else None      # 분석기가 아예 없으면 바로 선택을 안내한다
    sid2, h2 = started_apply(box2)
    box2.ex.script = [("failed", "", "AccessDenied")]
    box2.svc.handle(sid2, "bob", {"type": "approve_step", "version": 1, "step": "reboot", "hash": h2})
    box2.svc.handle(sid2, "bob", {"type": "run_step", "version": 1, "step": "reboot"})
    t2 = [m["text"] for m in box2.svc.view(sid2, "alice")["thread"] if m["role"] == "assistant"]
    assert any("실패했습니다" in t and "재시도, 건너뛰기, 중단 중에서 고르세요" in t for t in t2) and box2.svc.view(sid2, "alice")["analyzing"] is None
    box2.store.close(); box2.maps.close()


# ---------------- 이전 질문을 모델에게 함께 준다(실사용에서 발견: 짧은 답을 이해하지 못하고 같은 질문을 되풀이) ----------------
def test_the_previous_question_goes_to_the_model_with_each_answer(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, statuses=["clarify", "clarify", "plan"])
    c = box.svc.create("alice", "대화 맥락", "테스트 ec2 를 기본으로 해서 제일 작은 타입으로 생성해줘")
    sid = c["session_id"]
    box.svc.generate_plan(sid, "테스트 ec2 를 기본으로 해서 제일 작은 타입으로 생성해줘")
    assert box.svc.handle(sid, "alice", {"type": "answer", "text": "기본이라고 했잖아"})["ok"]
    assert wait_for(lambda: len(box.pipelines) == 2 and box.svc.view(sid, "alice")["pipeline"]["status"] == "clarify")
    first = box.pipelines[1].seen[0]
    assert "[이전 질문]" in first and "어느 것?" in first and first.index("[이전 질문]") < first.index("[추가 정보] 기본이라고 했잖아")     # 질문 → 답 순서
    assert box.svc.handle(sid, "alice", {"type": "answer", "text": "default vpc 에 default sg, ssh키는 없음, 우분투"})["ok"]
    assert wait_for(lambda: box.svc.view(sid, "alice")["pipeline"]["status"] == "plan")
    last = box.pipelines[2].seen[0]
    assert last.count("[이전 질문]") == 2 and last.count("[추가 정보]") == 2 and "ssh키는 없음" in last and "다시 시도" not in last
    assert last.startswith("테스트 ec2 를 기본으로")                                              # 첫 요청이 맨 앞
    box.store.close(); box.maps.close()


def test_old_questions_are_dropped_first_when_the_text_gets_long_and_stops_and_retries_are_not_questions(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, statuses=["clarify", "stopped", "plan"])
    c = box.svc.create("alice", "긴 대화", REQUEST)
    sid = c["session_id"]
    box.svc.generate_plan(sid, REQUEST)                                                          # clarify: 질문이 대화 기록에 kind=clarify로 남는다
    assert any(m.get("kind") == "clarify" for m in box.svc._thread(sid))
    long_answer = "자세한 설명 " * 120                                                           # 약 720자
    assert box.svc.handle(sid, "alice", {"type": "answer", "text": long_answer})["ok"]
    assert wait_for(lambda: box.svc.view(sid, "alice")["pipeline"]["status"] == "stopped")      # stopped의 안내문은 질문이 아니다
    assert box.svc.handle(sid, "alice", {"type": "retry_generation"})["ok"]
    assert wait_for(lambda: box.svc.view(sid, "alice")["pipeline"]["status"] == "plan")
    seen = box.pipelines[-1].seen[0]
    assert seen.count("[이전 질문]") == 1 and "계획 생성을 계속할 수 없어" not in seen and seen.count("[추가 정보]") == 1
    # 아주 긴 답이 겹치면 오래된 질문을 먼저 뺀다(질문이 없어도 답과 요청은 남는다)
    assert len(box.svc._combined(sid)) <= 1900
    box.store.close(); box.maps.close()
