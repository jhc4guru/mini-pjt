"""세션 저장소: 상태 전이 표, 멤버 토큰, plan 해시 검증, 승인 중복, 재시작 후 복원."""
import sqlite3

import pytest

from app.planner.schema import Phase, Plan, Step
from app.session.states import InvalidTransition, SESSION_TRANSITIONS, STEP_TRANSITIONS
from app.session.store import PlanTampered, SessionStore, StoreError


def make_plan(title="phase") -> Plan:
    steps = (Step(id="a", kind="cli", title="조회", command="aws ec2 describe-vpcs", op="read"),
             Step(id="b", kind="cli", title="변경", command="aws ec2 reboot-instances --instance-ids <INSTANCE_1>", op="mutate", depends_on=("a",)),
             Step(id="c", kind="cli", title="확인", command="aws ec2 describe-instance-status --instance-ids <INSTANCE_1>", op="read", check_for="b"))
    return Plan(summary="테스트", phases=(Phase(id="p1", title=title, steps=steps),))


@pytest.fixture()
def store(tmp_path):
    s = SessionStore(tmp_path / "s.sqlite")
    yield s
    s.close()


@pytest.fixture()
def sess(store):
    sid, invite, req = store.create_session("alice", "요청 <INSTANCE_1> 재부팅")
    return sid, invite, req


# ---------------- 참여자·토큰 ----------------
def test_tokens_are_stored_only_as_hashes_and_identify_the_member(store, sess, tmp_path):
    sid, invite, req = sess
    bob = store.join(sid, invite, "bob")
    assert store.authenticate(sid, req) == "alice" and store.authenticate(sid, bob) == "bob" and store.authenticate(sid, "nope") is None
    raw = (tmp_path / "s.sqlite").read_bytes()
    assert invite.encode() not in raw and req.encode() not in raw and bob.encode() not in raw
    assert store.role_of(sid, "alice") == "requester" and store.role_of(sid, "bob") == "approver"
    assert all("token" not in k for p in store.participants(sid) for k in p)


def test_join_needs_the_invite_and_a_free_valid_nickname(store, sess):
    sid, invite, _ = sess
    with pytest.raises(StoreError, match="invalid_invite"):
        store.join(sid, "wrong", "bob")
    store.join(sid, invite, "bob")
    for nick in ("BOB", "alice"):                                    # 대소문자 구분 없이 중복 불가, 요청자 닉네임도 불가
        with pytest.raises(StoreError, match="nickname_taken"):
            store.join(sid, invite, nick)
    for bad in ("", "a b", "x" * 21, "<script>", "a;b"):
        with pytest.raises(StoreError, match="invalid_nickname"):
            store.join(sid, invite, bad)


def test_a_member_token_of_another_session_does_not_authenticate(store, sess):
    sid, _, req = sess
    other, _, _ = store.create_session("carol", "다른 요청")
    assert store.authenticate(other, req) is None


# ---------------- 상태 전이 ----------------
def test_session_state_moves_only_along_the_table(store, sess):
    sid = sess[0]
    with pytest.raises(InvalidTransition):
        store.set_session_state(sid, "applying")                    # draft에서 바로 apply 불가
    store.set_session_state(sid, "plan_review")
    with pytest.raises(InvalidTransition):
        store.set_session_state(sid, "applying")                    # 승인 없이 apply 불가
    store.set_session_state(sid, "plan_approved")
    store.set_session_state(sid, "applying")
    store.set_session_state(sid, "completed")
    for dst in SESSION_TRANSITIONS:
        with pytest.raises(InvalidTransition):
            store.set_session_state(sid, dst)                       # 종료 상태에서는 어디로도 갈 수 없다
    with pytest.raises(StoreError, match="session_closed"):
        store.join(sid, sess[1], "late")


def test_every_state_in_the_tables_is_reachable_targets_are_known():
    for table in (SESSION_TRANSITIONS, STEP_TRANSITIONS):
        for src, dsts in table.items():
            assert dsts <= set(table), src


def test_step_cannot_skip_approval_or_leave_a_terminal_state(store, sess):
    sid = sess[0]
    v = store.add_plan_version(sid, make_plan())
    store.init_steps(sid, v, ["a", "b", "c"])
    with pytest.raises(InvalidTransition):
        store.transition_step(sid, v, "b", "running")               # 승인 없이 실행 상태로 갈 수 없다
    store.transition_step(sid, v, "b", "approved")
    store.transition_step(sid, v, "b", "running", count_attempt=True)
    store.transition_step(sid, v, "b", "done")
    for dst in STEP_TRANSITIONS:
        with pytest.raises(InvalidTransition):
            store.transition_step(sid, v, "b", dst)
    assert store.step_states(sid, v)["b"]["attempts"] == 1


def test_expected_state_prevents_a_second_concurrent_start(store, sess):
    sid = sess[0]
    v = store.add_plan_version(sid, make_plan())
    store.init_steps(sid, v, ["a"])
    store.transition_step(sid, v, "a", "approved")
    store.transition_step(sid, v, "a", "running", expect="approved")
    with pytest.raises(StoreError, match="unexpected_state:running"):
        store.transition_step(sid, v, "a", "running", expect="approved")


def test_failed_step_can_only_retry_skip_or_abort(store, sess):
    sid = sess[0]
    v = store.add_plan_version(sid, make_plan())
    store.init_steps(sid, v, ["a"])
    for s in ("approved", "running", "failed"):
        store.transition_step(sid, v, "a", s)
    with pytest.raises(InvalidTransition):
        store.transition_step(sid, v, "a", "done")                  # 실패를 성공으로 바꿀 수 없다
    store.transition_step(sid, v, "a", "pending")                   # 재시도: 재승인을 받는다


def test_carried_steps_start_done_and_unknown_carry_states_are_rejected(store, sess):
    sid = sess[0]
    v = store.add_plan_version(sid, make_plan())
    store.init_steps(sid, v, ["a", "b"], carry={"a": "done"})
    assert store.step_states(sid, v)["a"]["state"] == "done" and store.step_states(sid, v)["b"]["state"] == "pending"
    v2 = store.add_plan_version(sid, make_plan(), parent_version=v)
    with pytest.raises(StoreError, match="invalid_carry_state"):
        store.init_steps(sid, v2, ["a"], carry={"a": "running"})


# ---------------- plan 버전·해시 ----------------
def test_plan_round_trips_and_new_version_supersedes_the_old_one(store, sess):
    sid = sess[0]
    p = make_plan()
    v1 = store.add_plan_version(sid, p)
    assert store.load_plan(sid, v1) == p
    v2 = store.add_plan_version(sid, make_plan("다른 제목"), parent_version=v1)
    assert (v1, v2) == (1, 2) and store.plan_meta(sid, v1)["status"] == "superseded" and store.plan_meta(sid, v2)["parent_version"] == 1


def test_a_plan_edited_in_the_database_is_refused(store, sess, tmp_path):
    sid = sess[0]
    v = store.add_plan_version(sid, make_plan())
    db = sqlite3.connect(tmp_path / "s.sqlite")
    db.execute("UPDATE plan_versions SET plan_json = REPLACE(plan_json, 'reboot-instances', 'terminate-instances')")
    db.commit()
    db.close()
    with pytest.raises(PlanTampered):
        store.load_plan(sid, v)


# ---------------- 승인·이벤트 ----------------
def test_approval_is_unique_per_person_and_target_and_revocation_is_logged(store, sess):
    sid = sess[0]
    v = store.add_plan_version(sid, make_plan())
    assert store.add_approval(sid, v, "step", "b", "bob", "h") is True
    assert store.add_approval(sid, v, "step", "b", "BOB", "h") is False
    assert store.add_approval(sid, v, "step", "b", "carol", "h") is True
    with pytest.raises(StoreError, match="invalid_approval_kind"):
        store.add_approval(sid, v, "root", "b", "bob", "h")
    store.revoke_approvals(sid, v, "step", "b", nick="bob")
    assert store.approvals(sid, v, "step", "b") == []
    kinds = [e["kind"] for e in store.events(sid)]
    assert "approval:step" in kinds and "approval_revoked:step" in kinds


def test_events_record_state_changes_with_who_did_them(store, sess):
    sid = sess[0]
    store.set_session_state(sid, "plan_review", nick="alice")
    ev = [e for e in store.events(sid) if e["kind"] == "session_state"]
    assert ev[0]["nick"] == "alice" and ev[0]["detail"] == "draft->plan_review"


def test_state_survives_reopening_the_database(tmp_path):
    path = tmp_path / "s.sqlite"
    s1 = SessionStore(path)
    sid, invite, req = s1.create_session("alice", "요청")
    v = s1.add_plan_version(sid, make_plan())
    s1.init_steps(sid, v, ["a"])
    s1.transition_step(sid, v, "a", "approved")
    s1.close()
    s2 = SessionStore(path)
    assert s2.authenticate(sid, req) == "alice" and s2.load_plan(sid, v) == make_plan()
    assert s2.step_states(sid, v)["a"]["state"] == "approved"
    s2.close()


def test_a_database_made_before_titles_existed_is_upgraded_in_place(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, requester TEXT NOT NULL, state TEXT NOT NULL, masked_request TEXT NOT NULL, "
               "iac_managed INTEGER NOT NULL DEFAULT 0, invite_hash TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    db.execute("INSERT INTO sessions VALUES('s1','alice','draft','요청',0,'h',1,1)")
    db.commit(); db.close()
    s = SessionStore(path)
    assert s.session("s1")["title"] == ""
    sid, _, _ = s.create_session("bob", "요청", title="새 제목")
    assert s.session(sid)["title"] == "새 제목"
    s.close()
