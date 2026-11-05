"""반려(D-51): 확인자가 코멘트와 함께 plan을 돌려보내고, 요청자가 연습장에서 고쳐 다시 검토를 요청한다."""
import pytest
from test_apply import chunks, snapshot  # noqa: F401
from test_service import REQUEST, build

from app.session.intents import parse_intent


@pytest.fixture()
def flow(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot)
    c = box.svc.create("alice", "반려 확인", REQUEST)
    sid = c["session_id"]
    box.svc.generate_plan(sid, REQUEST)
    box.svc.join(sid, c["invite"], "bob")
    yield box, sid
    box.store.close(); box.maps.close()


def rej(box, sid, who, summary="범위가 넓습니다", steps=None, **kw):
    v = box.svc.view(sid, who)
    return box.svc.handle(sid, who, {"type": "reject_plan", "version": v["version"], "hash": v["plan_hash"], "summary": summary, "steps": steps if steps is not None else {}, **kw})


def test_the_reviewer_rejects_with_comments_and_the_session_returns_to_the_pad(flow):
    box, sid = flow
    r = rej(box, sid, "bob", "재부팅 범위를 줄여 주세요", {"reboot": "대상 인스턴스를 하나로"})
    assert r["ok"]
    v = box.svc.view(sid, "alice")
    assert v["session"]["state"] == "draft" and v["plan_status"] == "rejected"
    assert v["rejection"]["by"] == "bob" and v["rejection"]["summary"] == "재부팅 범위를 줄여 주세요" and v["rejection"]["steps"] == {"reboot": "대상 인스턴스를 하나로"}
    assert any(m["role"] == "assistant" and "bob님이 plan을 반려했습니다 (절차 코멘트 1건)" in m["text"] for m in v["thread"])         # 대화에도 남는다
    assert v["session"]["times"]["rejected"] is not None


def test_only_a_non_requester_can_reject_and_only_before_apply_and_with_a_comment(flow):
    box, sid = flow
    assert rej(box, sid, "alice")["code"] == "requester_cannot_reject_plan"
    assert rej(box, sid, "bob", "   ")["code"] == "comment_required"
    assert rej(box, sid, "bob", "x" * 501)["code"] == "comment_too_long"
    assert rej(box, sid, "bob", "ok", {"nope": "없는 절차"})["code"] == "unknown_step"
    v = box.svc.view(sid, "bob")
    assert box.svc.handle(sid, "bob", {"type": "reject_plan", "version": v["version"], "hash": "0" * 64, "summary": "x", "steps": {}})["code"] == "plan_hash_mismatch"
    assert box.svc.view(sid, "alice")["session"]["state"] == "plan_review"                       # 거부된 시도는 아무것도 바꾸지 않는다
    box.svc.handle(sid, "bob", {"type": "approve_plan", "version": v["version"], "hash": v["plan_hash"]})
    box.svc.handle(sid, "alice", {"type": "start_apply"})
    assert rej(box, sid, "bob")["code"].startswith("plan_not_rejectable")                         # apply가 시작된 뒤에는 반려가 아니라 일시정지·중단


def test_an_approved_plan_can_still_be_rejected_before_apply_and_then_cannot_start(flow):
    box, sid = flow
    v = box.svc.view(sid, "bob")
    box.svc.handle(sid, "bob", {"type": "approve_plan", "version": v["version"], "hash": v["plan_hash"]})
    assert rej(box, sid, "bob", "다시 보니 범위가 넓습니다")["ok"]
    assert box.svc.view(sid, "alice")["plan_status"] == "rejected"
    assert box.svc.handle(sid, "alice", {"type": "start_apply"})["ok"] is False                   # 반려된 plan으로는 apply를 시작할 수 없다


def test_comments_are_masked_and_secrets_are_blocked(flow):
    box, sid = flow
    assert rej(box, sid, "bob", "password=hunter2 로 바꾸세요")["ok"] is False
    r = rej(box, sid, "bob", "i-0abc123def4567890 는 빼 주세요", {"reboot": "계정 123456789012 확인"})
    assert r["ok"]
    raw = box.store.get_meta(sid, "rejection")
    assert "i-0abc123def4567890" not in raw and "123456789012" not in raw                         # 저장은 마스킹된 형태
    assert "i-0abc123def4567890" in box.svc.view(sid, "alice")["rejection"]["summary"]            # 화면에서만 실제 값으로 복원


def test_the_requester_fixes_the_pad_and_asks_again_then_the_old_comments_are_cleared(flow):
    box, sid = flow
    rej(box, sid, "bob", "다시 검토가 필요합니다", {"reboot": "범위 축소"})
    v = box.svc.view(sid, "alice")
    assert v["rejection"] is not None and v["session"]["state"] == "draft"
    assert box.svc.handle(sid, "alice", {"type": "send_to_apply"})["ok"]
    v2 = box.svc.view(sid, "alice")
    assert v2["session"]["state"] == "plan_review" and v2["version"] == v["version"] + 1 and v2["rejection"] is None and v2["plan_status"] == "draft"
    assert any("반려했습니다" in m["text"] for m in v2["thread"])                                  # 지난 반려는 대화 기록에 남는다
    assert box.svc.handle(sid, "bob", {"type": "approve_plan", "version": v2["version"], "hash": v2["plan_hash"]})["ok"]       # 새 버전은 다시 확정(승인)할 수 있다


def test_rejecting_by_talking_is_a_card_whose_comment_is_the_message(flow):
    box, sid = flow
    assert parse_intent("범위가 넓어서 반려해줘").kind == "reject_plan" and parse_intent("이 부분 다시 검토해 주세요").kind == "reject_plan"
    assert parse_intent("제목 수정해줘") is None
    box.svc.handle(sid, "alice", {"type": "propose_edit", "text": "반려해줘"})
    assert "자기 plan을 반려할 수 없습니다" in box.svc.view(sid, "alice")["thread"][-1]["text"]
    box.svc.handle(sid, "bob", {"type": "propose_edit", "text": "범위가 넓어서 반려해줘"})
    p = box.svc.view(sid, "bob")["proposal"]
    assert p["action"]["type"] == "reject_plan" and p["action"]["who"] == "approver" and box.svc.view(sid, "bob")["session"]["state"] == "plan_review"
    assert box.svc.handle(sid, "bob", {"type": "accept_proposal", "proposal": p["id"]})["ok"]
    v = box.svc.view(sid, "alice")
    assert v["session"]["state"] == "draft" and v["rejection"]["summary"] == "범위가 넓어서 반려해줘"
