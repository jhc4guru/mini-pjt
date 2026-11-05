"""웹 계층: 생성·초대·WebSocket 인증과 동기화, Origin 검사, 메시지 상한, 닉네임 위조 방지."""
import json
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_apply import INSTANCE, chunks, snapshot  # noqa: F401  (픽스처 재사용)
from test_service import REQUEST, build

from app.web.server import MAX_MESSAGE_BYTES, create_app


@pytest.fixture()
def web(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot)
    app = create_app(box.svc)
    with TestClient(app) as client:
        yield client, box
    box.store.close()
    box.maps.close()


def create(client, nick="alice", text=REQUEST):
    r = client.post("/api/sessions", json={"nick": nick, "title": "테스트 작업", "request": text})
    assert r.status_code == 200, r.text
    return r.json()


def auth(ws, token):
    ws.send_text(json.dumps({"type": "auth", "token": token}))
    return ws.receive_json()


def recv_view(ws, until=lambda v: True, tries=60):
    for _ in range(tries):
        m = ws.receive_json()
        if m["type"] == "view" and until(m["data"]):
            return m["data"]
    raise AssertionError("원하는 스냅샷이 오지 않았다")


def test_create_rejects_unsafe_input_and_returns_tokens_otherwise(web):
    client, _ = web
    r = client.post("/api/sessions", json={"nick": "alice", "title": "테스트 작업", "request": "password=hunter2 로 사용자 만들어"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "input_rejected" and "hunter2" not in r.text
    assert client.post("/api/sessions", json={"nick": "a b", "title": "테스트 작업", "request": REQUEST}).status_code == 400
    assert client.post("/api/sessions", json={"nick": "alice", "title": "테스트 작업", "request": REQUEST, "extra": 1}).status_code == 422
    c = create(client)
    assert c["member_token"] and c["invite"] in c["join_path"] and INSTANCE not in json.dumps(c)


def test_join_needs_the_invite_and_a_free_nickname(web):
    client, _ = web
    c = create(client)
    sid = c["session_id"]
    assert client.post(f"/api/sessions/{sid}/join", json={"invite": "wrong", "nick": "bob"}).status_code == 400
    assert client.post(f"/api/sessions/{sid}/join", json={"invite": c["invite"], "nick": "bob"}).status_code == 200
    assert client.post(f"/api/sessions/{sid}/join", json={"invite": c["invite"], "nick": "bob"}).json()["detail"]["code"] == "nickname_taken"


def test_websocket_needs_a_valid_token_first(web):
    client, _ = web
    c = create(client)
    with client.websocket_connect(f"/ws/{c['session_id']}") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "wrong"}))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401
    with client.websocket_connect(f"/ws/{c['session_id']}") as ws:
        ws.send_text(json.dumps({"type": "approve_plan", "version": 1, "hash": "x"}))      # 인증 전 동작은 받지 않는다
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_a_foreign_origin_is_refused_for_websocket_and_rest(web):
    client, _ = web
    c = create(client)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/ws/{c['session_id']}", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()
    assert e.value.code == 4403
    r = client.post("/api/sessions", json={"nick": "x", "title": "테스트 작업", "request": REQUEST}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    with client.websocket_connect(f"/ws/{c['session_id']}", headers={"origin": "http://testserver"}) as ws:     # 같은 호스트는 허용
        assert auth(ws, c["member_token"])["type"] == "view"


def test_two_tabs_see_the_same_state_and_the_action_owner_is_the_token_not_the_message(web):
    client, box = web
    c = create(client)
    sid = c["session_id"]
    bob = client.post(f"/api/sessions/{sid}/join", json={"invite": c["invite"], "nick": "bob"}).json()["member_token"]
    with client.websocket_connect(f"/ws/{sid}") as a, client.websocket_connect(f"/ws/{sid}") as b:
        assert auth(a, c["member_token"])["data"]["you"]["nick"] == "alice"
        assert auth(b, bob)["data"]["you"]["nick"] == "bob"
        va = recv_view(a, lambda v: v.get("plan_hash"))                                                  # 계획 생성(백그라운드) 후 스냅샷
        h = va["plan_hash"]
        a.send_text(json.dumps({"type": "approve_plan", "version": 1, "hash": h, "nick": "bob", "id": "1"}))   # 닉네임을 속여도 소용없다
        res = next(m for m in iter(a.receive_json, None) if m["type"] == "result")
        assert res == {"type": "result", "id": "1", "ok": False, "code": "requester_cannot_approve_plan"}
        b.send_text(json.dumps({"type": "approve_plan", "version": 1, "hash": h, "id": "2"}))
        vb = recv_view(b, lambda v: v["session"]["state"] == "plan_approved")
        assert INSTANCE in json.dumps(vb["plan"], ensure_ascii=False)                                    # 화면에는 실제 값
        va2 = recv_view(a, lambda v: v["session"]["state"] == "plan_approved")                           # 다른 탭도 같은 상태를 받는다
        assert va2["plan_hash"] == h


def test_bad_and_oversized_messages_get_codes_and_do_not_kill_the_connection(web):
    client, _ = web
    c = create(client)
    with client.websocket_connect(f"/ws/{c['session_id']}") as ws:
        auth(ws, c["member_token"])
        ws.send_text("not json")
        assert next(m for m in iter(ws.receive_json, None) if m["type"] == "result")["code"] == "bad_json"
        ws.send_text("[1,2]")
        assert next(m for m in iter(ws.receive_json, None) if m["type"] == "result")["code"] == "bad_json"
        ws.send_text("x" * (MAX_MESSAGE_BYTES + 1))
        assert next(m for m in iter(ws.receive_json, None) if m["type"] == "result")["code"] == "message_too_large"
        ws.send_text(json.dumps({"type": "nope"}))
        assert next(m for m in iter(ws.receive_json, None) if m["type"] == "result")["code"] == "unknown_message"


def test_health_and_index_placeholder(web):
    client, _ = web
    assert client.get("/api/health").json() == {"ok": True}
    r = client.get("/")
    assert r.status_code == 200 and r.headers["x-content-type-options"] == "nosniff"


def test_history_is_read_from_the_database_with_the_tokens_the_browser_holds(web):
    client, _ = web
    a = create(client, "alice")
    other = create(client, "carol")
    r = client.post("/api/history", json={"items": [{"sid": a["session_id"], "token": a["member_token"]}, {"sid": other["session_id"], "token": a["member_token"]},
                                                     {"sid": "nope", "token": "x"}]})
    assert r.status_code == 200
    items = r.json()["items"]
    good = [i for i in items if i["valid"]]
    assert len(good) == 1 and good[0]["title"] == "테스트 작업" and good[0]["nick"] == "alice" and "token" not in good[0]
    assert len([i for i in items if not i["valid"]]) == 2 and all(set(i) == {"sid", "valid"} for i in items if not i["valid"])
    assert client.post("/api/history", json={"items": [{"sid": "x", "token": "y", "extra": 1}]}).status_code == 422
    assert client.post("/api/history", json={"items": [{"sid": "x", "token": "y"}] * 51}).status_code == 422                 # 한 번에 50개까지
    assert client.post("/api/history", json={"items": []}, headers={"origin": "http://evil.example"}).status_code == 403


def test_the_requester_gets_a_fresh_invite_over_the_websocket_and_only_the_requester(web):
    client, _ = web
    c = create(client)
    sid = c["session_id"]
    bob = client.post(f"/api/sessions/{sid}/join", json={"invite": c["invite"], "nick": "bob"}).json()["member_token"]
    with client.websocket_connect(f"/ws/{sid}") as a, client.websocket_connect(f"/ws/{sid}") as b:
        auth(a, c["member_token"]); auth(b, bob)
        a.send_text(json.dumps({"type": "create_invite", "id": "i1"}))
        res = next(m for m in iter(a.receive_json, None) if m["type"] == "result")
        assert res["ok"] and res["invite"] and res["invite"] != c["invite"]
        b.send_text(json.dumps({"type": "create_invite", "id": "i2"}))
        res2 = next(m for m in iter(b.receive_json, None) if m["type"] == "result")
        assert res2 == {"type": "result", "id": "i2", "ok": False, "code": "invite_by_requester_only"}
    assert client.post(f"/api/sessions/{sid}/join", json={"invite": c["invite"], "nick": "carol"}).status_code == 400            # 이전 링크는 무효
    assert client.post(f"/api/sessions/{sid}/join", json={"invite": res["invite"], "nick": "carol"}).status_code == 200
