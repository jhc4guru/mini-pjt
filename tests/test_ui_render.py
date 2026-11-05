"""화면 렌더링: 실제 서비스가 만든 스냅샷을 가짜 DOM(node)에서 그려 예외 없이 그려지고, 3분할(이력·절차·대화)의 내용, 역할·상태에 맞는 버튼,
드래그앤드롭 순서 변경 메시지를 확인한다.

실제 브라우저가 아니라 `tests/ui_harness.js`의 가짜 DOM이다(레이아웃·스타일은 확인하지 않는다). 실제 Chrome 확인은 `tests/browser/`(수동).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_apply import INSTANCE, chunks, snapshot  # noqa: F401
from test_service import REQUEST, build

ROOT = Path(__file__).resolve().parent.parent


def snap(box, sid, name, nick, **kw):
    return {"name": name, "view": box.svc.view(sid, nick), **kw}


@pytest.fixture()
def rendered(tmp_path, chunks, snapshot):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node가 필요합니다")
    manual = ({"id": "man", "kind": "manual", "title": "직접 실행", "command": "kubectl exec web-0 -- ls"},
              {"id": "man-chk", "kind": "cli", "title": "확인", "command": "aws ec2 describe-vpcs", "check_for": "man"})
    box = build(tmp_path, chunks, snapshot, extra=manual)
    svc = box.svc
    c = svc.create("alice", "EKS 재부팅 작업", REQUEST)
    sid = c["session_id"]
    svc.generate_plan(sid, REQUEST)
    svc.join(sid, c["invite"], "bob")
    hist = svc.history([{"sid": sid, "token": c["member_token"]}])
    def entry(state, n, title):
        return {"sid": title, "valid": True, "nick": "alice", "title": title, "state": state, "requester": "alice", "created_at": 1, "updated_at": 2, "steps": {}, "risk": None, "version": 1, "participants": n}
    dots = snap(box, sid, "history_dots", "alice", history=[entry("plan_review", 1, "작성중"), entry("plan_approved", 2, "대기"), entry("applying", 2, "진행"), entry("applying", 1, "진행-초대전"),
                                                            entry("paused", 2, "멈춤"), entry("completed", 2, "완료"), entry("aborted", 2, "중단")])
    cases = [dots, snap(box, sid, "review_alice", "alice", history=hist, sel="reboot"), snap(box, sid, "review_bob", "bob"),
             snap(box, sid, "invite_alice", "alice", invite="http://x/?s=1&i=abc"), snap(box, sid, "plan_rows", "alice"),
             snap(box, sid, "insert_after", "alice", clicks=["＋", "추가"])]
    analyzing = snap(box, sid, "analyzing_alice", "alice")
    analyzing["view"]["analyzing"] = "reboot"
    cases.append(analyzing)
    h = svc.view(sid, "alice")["plan_hash"]
    svc.handle(sid, "bob", {"type": "approve_plan", "version": 1, "hash": h})
    cases.append(snap(box, sid, "approved", "alice", click="apply 진행 →", ctab="apply"))
    svc.handle(sid, "alice", {"type": "start_apply"})
    cases.append(snap(box, sid, "applying_pending_alice", "alice", sel="reboot", ctab="apply"))
    cases.append(snap(box, sid, "applying_pending_bob", "bob", ctab="apply"))
    cases.append(snap(box, sid, "applying_read_alice", "alice", sel="man-chk", ctab="apply", click="확인(실행)"))
    cases.append(snap(box, sid, "applying_plan_tab", "alice", sel="reboot", ctab="plan"))
    svc.handle(sid, "bob", {"type": "approve_step", "version": 1, "step": "reboot", "hash": h})
    cases.append(snap(box, sid, "applying_approved", "bob", sel="reboot", click="실행", ctab="apply"))
    box.ex.script = [("failed", "", "An error occurred (ThrottlingException)")]
    svc.handle(sid, "bob", {"type": "run_step", "version": 1, "step": "reboot"})
    cases.append(snap(box, sid, "failed_paused", "alice", sel="reboot", click="재시도", ctab="apply"))
    svc.handle(sid, "alice", {"type": "decide", "version": 1, "step": "reboot", "choice": "retry"})
    svc.handle(sid, "bob", {"type": "approve_step", "version": 1, "step": "reboot", "hash": h})
    svc.handle(sid, "bob", {"type": "run_step", "version": 1, "step": "reboot"})
    svc.handle(sid, "alice", {"type": "approve_step", "version": 1, "step": "man", "hash": h})
    cases.append(snap(box, sid, "manual_report_alice", "alice", sel="man", ctab="apply"))
    svc.handle(sid, "alice", {"type": "report_done", "version": 1, "step": "man", "text": "완료"})
    cases.append(snap(box, sid, "manual_confirm_bob", "bob", sel="man", ctab="apply"))
    cases.append(snap(box, sid, "manual_confirm_alice", "alice", sel="man", ctab="apply"))
    # 되묻기(같은 대화에서 이어서 답함)와 생성 중
    (tmp_path / "b2").mkdir()
    box2 = build(tmp_path / "b2", chunks, snapshot, pipeline_status="clarify")
    c2 = box2.svc.create("alice", "되묻기 작업", REQUEST)
    cases.append(snap(box2, c2["session_id"], "generating_alice", "alice"))
    box2.svc.generate_plan(c2["session_id"], REQUEST)
    box2.svc.join(c2["session_id"], c2["invite"], "bob")
    cases.append(snap(box2, c2["session_id"], "clarify_alice", "alice", click="답하기"))
    cases.append(snap(box2, c2["session_id"], "clarify_bob", "bob"))
    # 계획 생성이 중단된 세션: 다시 시도
    (tmp_path / "b4").mkdir()
    box4 = build(tmp_path / "b4", chunks, snapshot, pipeline_status="stopped")
    c4 = box4.svc.create("alice", "중단 작업", REQUEST)
    box4.svc.generate_plan(c4["session_id"], REQUEST)
    box4.svc.join(c4["session_id"], c4["invite"], "bob")
    cases.append(snap(box4, c4["session_id"], "stopped_alice", "alice", click="다시 시도"))
    cases.append(snap(box4, c4["session_id"], "stopped_bob", "bob"))
    # 계획 검토 중 끌어서 놓기
    (tmp_path / "b3").mkdir()
    box3 = build(tmp_path / "b3", chunks, snapshot, extra=({"id": "peek", "kind": "cli", "title": "조회", "command": "aws ec2 describe-vpcs"},))
    c3 = box3.svc.create("alice", "정렬 작업", REQUEST)
    box3.svc.generate_plan(c3["session_id"], REQUEST)
    cases.append(snap(box3, c3["session_id"], "drag_move", "alice", drag={"from": "peek", "to": "reboot"}))
    cases.append(snap(box3, c3["session_id"], "drag_end", "alice", drag={"from": "reboot", "end": "p1"}))
    cases.append(snap(box3, c3["session_id"], "drag_same", "alice", drag={"from": "reboot", "to": "reboot"}))
    f = tmp_path / "cases.json"
    f.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    out = subprocess.run([node, str(ROOT / "tests" / "ui_harness.js"), str(ROOT / "app" / "static" / "index.html"), str(f)], capture_output=True, text=True, encoding="utf-8")
    for b in (box, box2, box3, box4):
        b.store.close()
        b.maps.close()
    assert out.returncode == 0, out.stderr[:800]
    return json.loads(out.stdout)


def btn(r, name, text, area="buttons"):
    return next((b for b in r[name][area] if b["text"] == text), None)


# ---------------- 3분할 구조 ----------------
def test_the_left_column_has_new_task_and_a_one_line_history_from_the_db(rendered):
    left = rendered["review_alice"]["left"]
    assert "＋ 새 작업" in [b["text"] for b in rendered["review_alice"]["leftButtons"]]
    assert "EKS 재부팅 작업" in left and "서버 DB" in left
    assert "plan 검토" not in left and "위험 중간" not in left and "대기 4" not in left                                      # 이력은 한 줄(제목 + 상태 점)이다. 자세한 내용은 툴팁
    assert "초대 링크" not in left and "● alice" not in left                                                                  # 작업자·초대는 제목 아래(중앙)로 옮겼다


def test_history_status_dots_follow_the_stage_of_the_task(rendered):
    d = rendered["history_dots"]["dots"]
    cls = [x["cls"] for x in d][:7]
    assert cls == ["dot plan", "dot plan", "dot live", "dot livewait", "dot paused", "dot done", "dot aborted"]
    labels = [x["label"] for x in d][:7]
    assert labels[0] == "PLAN 작성 중" and labels[1] == "PLAN 완료 · APPLY 대기" and labels[2] == "APPLY 진행 중 (작업자 초대 완료)"
    assert labels[3] == "APPLY 진행 중 (작업자 초대 필요)" and labels[5] == "APPLY 작업 완료" and labels[6] == "중단됨"
    left = rendered["history_dots"]["left"]
    assert "APPLY 진행 중(작업자 초대 완료)" in left and "PLAN 작성 중 · APPLY 대기" in left                                      # 범례


def test_workers_and_the_invite_link_sit_under_the_title_and_only_the_requester_can_make_a_link(rendered):
    c = rendered["review_alice"]["center"]
    assert c.index("EKS 재부팅 작업") < c.index("작업자") < c.index("Plan") and "alice (요청자)" in c and "bob (승인자)" in c
    assert "초대 링크 만들기" in [b["text"] for b in rendered["review_alice"]["buttons"]]
    assert "초대 링크 만들기" not in [b["text"] for b in rendered["review_bob"]["buttons"]]
    assert "복사" in [b["text"] for b in rendered["invite_alice"]["buttons"]]


def test_the_center_shows_the_title_and_the_ordered_procedure_list_with_the_log_folded(rendered):
    c = rendered["review_alice"]["center"]
    assert "EKS 재부팅 작업" in c and "Plan" in c and "활동 로그" in c and INSTANCE in c
    assert {"reboot", "verify"} <= set(rendered["plan_rows"]["rows"]) and rendered["plan_rows"]["rows"].index("reboot") < rendered["plan_rows"]["rows"].index("verify")


def test_the_right_column_is_the_conversation_with_the_composer_below(rendered):
    t = rendered["clarify_alice"]["chat"]
    assert "요청" in t and "에이전트" in t and "어느 것?" in t and rendered["clarify_alice"]["composer"]["buttons"][0]["text"] == "답하기"


def test_a_new_task_asks_for_the_title_in_the_center_and_the_prompt_in_the_chat(rendered):
    n = rendered["newtask"]
    assert "새 작업" in n["center"] and "제목" in n["center"] and "닉네임" not in n["chat"]
    assert "어떤 작업을 하시겠어요" in n["chat"] and "작업을 설명해" in n["composer"]["placeholder"] and n["composer"]["buttons"][0]["text"] == "보내기"
    w = rendered["welcome"]
    assert "＋ 새 작업" in w["center"] and w["composer"] == 0


# ---------------- 승인·역할 ----------------
def test_plan_approval_button_is_off_for_the_requester_and_on_for_the_other_person(rendered):
    assert btn(rendered, "review_alice", "plan 일괄 승인")["disabled"] is True
    assert btn(rendered, "review_bob", "plan 일괄 승인")["disabled"] is False
    assert btn(rendered, "approved", "apply 진행 →") is not None and btn(rendered, "review_bob", "apply 진행 →") is None
    assert btn(rendered, "approved", "apply 시작") is None                                                                       # Apply 탭이 아직 없으므로 시작 버튼은 Plan 탭의 "apply 진행 →" 하나뿐


def test_step_approval_is_off_for_the_requester_on_change_steps(rendered):
    approvals = [b for b in rendered["applying_pending_alice"]["buttons"] if b["text"] == "승인"]
    assert approvals and approvals[0]["disabled"] is True                                                             # 선택한 변경 step
    assert btn(rendered, "applying_approved", "실행") is not None


def test_run_button_sends_the_run_message_with_the_visible_version_and_step(rendered):
    assert rendered["applying_approved"]["sent"] == [{"type": "run_step", "version": 1, "step": "reboot"}]


def test_failure_shows_the_three_choices_and_sends_the_choice(rendered):
    r = rendered["failed_paused"]
    for label in ("재시도", "건너뛰기", "중단"):
        assert btn(rendered, "failed_paused", label) is not None
    assert "실패했습니다" in r["center"] and "throttled" in r["center"]
    assert [{k: v for k, v in m.items() if k != "state_checked"} for m in r["sent"]] == [{"type": "decide", "version": 1, "step": "reboot", "choice": "retry"}]


def test_manual_flow_shows_the_report_to_the_requester_and_the_confirmation_to_the_other_person(rendered):
    assert btn(rendered, "manual_report_alice", "실행 완료 보고") is not None and "서버는 이 명령을 실행하지 않습니다" in rendered["manual_report_alice"]["center"]
    assert btn(rendered, "manual_confirm_bob", "확인(완료)") is not None and "요청자의 보고" in rendered["manual_confirm_bob"]["center"]
    assert btn(rendered, "manual_confirm_alice", "확인(완료)") is None and "다른 작업자의 확인을 기다립니다" in rendered["manual_confirm_alice"]["center"]


# ---------------- 대화 입력창 ----------------
def test_generating_blocks_the_input_and_a_clarification_is_answered_by_the_requester_only(rendered):
    g = rendered["generating_alice"]["composer"]
    assert g["disabled"] is True and "답변을 생성 중입니다" in g["placeholder"]
    a, b = rendered["clarify_alice"]["composer"], rendered["clarify_bob"]["composer"]
    assert a["disabled"] is False and "이어서 적어" in a["placeholder"] and b["disabled"] is True and "요청자가 답할 때까지" in b["placeholder"]
    assert rendered["clarify_alice"]["sent"] == []                                                                    # 빈 입력은 보내지 않는다
    assert rendered["review_alice"]["composer"]["disabled"] is True and "절차 목록에서 검토" in rendered["review_alice"]["composer"]["placeholder"]


# ---------------- 드래그앤드롭 ----------------
def test_steps_are_draggable_only_while_the_plan_can_be_edited(rendered):
    assert set(rendered["review_alice"]["draggable"]) == set(rendered["review_alice"]["rows"]) and rendered["review_alice"]["rows"]
    assert rendered["applying_pending_alice"]["draggable"] == [] and rendered["applying_plan_tab"]["draggable"] == []           # apply 중에는 어느 탭에서도 끌 수 없다


def test_dropping_a_step_on_another_sends_a_move_with_the_step_it_should_follow(rendered):
    sent = rendered["drag_move"]["sent"]
    rows = rendered["drag_move"]["rows"]
    assert len(sent) == 1 and sent[0]["type"] == "edit" and sent[0]["op"] == "move_step" and sent[0]["step_id"] == "peek"
    target = rows.index("reboot")
    assert sent[0]["after"] == (rows[target - 1] if target > 0 else None)


def test_dropping_on_the_end_zone_and_on_itself(rendered):
    end, rows = rendered["drag_end"]["sent"], rendered["drag_end"]["rows"]
    assert len(end) == 1 and end[0] == {"type": "edit", "op": "move_step", "step_id": "reboot", "after": rows[-1]}
    assert rendered["drag_same"]["sent"] == []                                                                         # 자기 자신 위에 놓으면 아무것도 보내지 않는다


def test_a_stopped_generation_offers_retry_to_the_requester_and_sends_it(rendered):
    comp = rendered["stopped_alice"]["composer"]
    assert [b["text"] for b in comp["buttons"]] == ["답하기", "다시 시도"] and comp["disabled"] is False
    assert "다시 시도" in comp["placeholder"] and "＋ 새 작업" in comp["placeholder"]          # 이 작업의 추가 정보만, 다른 작업은 새 작업
    assert rendered["stopped_alice"]["sent"] == [{"type": "retry_generation"}]
    assert [b["text"] for b in rendered["stopped_bob"]["composer"]["buttons"]] == ["답하기"] and rendered["stopped_bob"]["composer"]["disabled"] is True


# ---------------- Plan / Apply 탭 ----------------
def test_the_apply_tab_appears_only_after_the_plan_is_approved(rendered):
    assert [x["text"] for x in rendered["review_alice"]["tabs"]] == ["Plan"]
    assert [x["text"] for x in rendered["approved"]["tabs"]] == ["Plan"]                                                    # 승인만으로는 Apply가 생기지 않는다(apply를 진행해야 생성)
    assert [x["text"] for x in rendered["applying_pending_alice"]["tabs"]] == ["Plan", "Apply"] and rendered["applying_pending_alice"]["tabs"][1]["on"] is True


def test_moving_to_apply_sends_start_and_the_apply_tab_has_no_edit_controls(rendered):
    assert rendered["approved"]["sent"] == [{"type": "start_apply"}]
    a = rendered["applying_pending_alice"]
    assert "Apply — 절차 실행" in a["center"] and "수정 불가" in a["center"]
    texts = [b["text"] for b in a["buttons"]]
    assert "수정 저장" not in texts and "＋" not in texts and not any("절차 추가" in x for x in texts) and "삭제" not in texts          # 수정·삽입·삭제는 없다
    p = rendered["applying_plan_tab"]["buttons"]
    assert "승인" not in [b["text"] for b in p] and "실행" not in [b["text"] for b in p]                                       # Plan 탭에는 실행 버튼이 없다


# ---------------- [AGENT] / [MANUAL] 구분 ----------------
def test_each_step_is_labeled_by_who_executes_it(rendered):
    a = rendered["plan_rows"]["actors"]
    assert a["reboot"] == "[AGENT]" and a["verify"] == "[AGENT]" and a["man"] == "[MANUAL]" and a["man-chk"] == "[AGENT]"
    assert "[AGENT]" in rendered["plan_rows"]["center"] and "[MANUAL]" in rendered["plan_rows"]["center"] and "서버가 실행" in rendered["plan_rows"]["center"]


# ---------------- 읽기 절차는 확인으로 ----------------
def test_read_steps_get_a_single_action_icon_and_changes_keep_approve_then_run_icons(rendered):
    r = rendered["applying_read_alice"]
    assert [i["label"] for i in r["rowIcons"]["man-chk"]] == ["확인(실행)"] and r["rowIcons"]["man-chk"][0]["disabled"] is False
    assert [i["label"] for i in r["rowIcons"]["reboot"]] == ["승인", "실행"]
    assert r["sent"] == [{"type": "run_read", "version": 1, "step": "verify", "hash": r["sent"][0]["hash"]}] and len(r["sent"][0]["hash"]) == 64     # 화면 순서상 첫 읽기 아이콘
    labels = [b["text"] for b in rendered["applying_read_alice"]["buttons"]]
    assert labels.count("확인(실행)") == 2 and "확인" not in labels and "승인" in labels                                                  # 줄의 아이콘뿐(펼친 상세에는 승인·확인 버튼이 없다)


def test_icons_show_approval_as_a_checkbox_and_execution_as_an_action_and_change_when_done(rendered):
    a = rendered["applying_pending_alice"]["rowIcons"]["reboot"]                                                         # 요청자: 변경 절차는 승인할 수 없다
    assert [(i["glyph"], i["disabled"]) for i in a] == [("☐", True), ("▶", True)]
    b = rendered["applying_pending_bob"]["rowIcons"]["reboot"]                                                           # 다른 작업자: 승인할 수 있고 실행은 승인 뒤
    assert [(i["glyph"], i["disabled"]) for i in b] == [("☐", False), ("▶", True)]
    c = rendered["applying_approved"]["rowIcons"]["reboot"]                                                              # 승인 뒤: ☑(승인됨) + 실행 가능
    assert [(i["glyph"], i["tag"], i["disabled"]) for i in c] == [("☑", "span", False), ("▶", "button", False)]
    assert [i["glyph"] for i in rendered["manual_report_alice"]["rowIcons"]["reboot"]] == ["✔"]                          # 실행되면 ✔
    assert [i["label"] for i in rendered["manual_report_alice"]["rowIcons"]["man"]] == ["보고 작성"]


def test_state_texts_are_replaced_by_icons_in_the_rows(rendered):
    rows = rendered["applying_pending_alice"]["center"]
    assert "중단됨" not in rows.split("실행 로그")[0]
    p = rendered["applying_pending_alice"]
    assert "대기" not in "".join(i["label"] or "" for i in p["rowIcons"]["reboot"])


def test_the_terminal_shows_commands_and_results_in_order(rendered):
    f = rendered["failed_paused"]
    assert "실행 로그 · CLI 결과 (터미널)" in f["center"]
    assert "$ aws ec2 reboot-instances" in f["terminal"] and "ThrottlingException" in f["terminal"] and "✖ failed" in f["terminal"] and "[AGENT] reboot" in f["terminal"]
    assert "아직 실행 기록이 없습니다" in rendered["applying_pending_alice"]["center"] and rendered["applying_pending_alice"]["terminal"] == ""


# ---------------- 중간에 끼워 넣기 ----------------
def test_a_step_can_be_inserted_right_after_a_chosen_step(rendered):
    r = rendered["insert_after"]
    assert "절차 끼워 넣기 — " in r["centerAfter"] or r["sent"]                                    # ＋를 누르면 그 줄 바로 아래에 양식이 열린다
    assert len(r["sent"]) == 1 and r["sent"][0]["op"] == "add_steps" and r["sent"][0]["type"] == "edit"
    assert r["sent"][0]["after"] == rendered["insert_after"]["rows"][0] and r["sent"][0]["phase"] == "p1"


# ---------------- 대화는 작업별, 분석 중 표시 ----------------
def test_the_conversation_header_names_the_task_and_shows_a_generating_indicator_during_analysis(rendered):
    assert "대화 · EKS 재부팅 작업" in rendered["review_alice"]["chat"] and "대화 · 되묻기 작업" in rendered["clarify_alice"]["chat"]
    a = rendered["analyzing_alice"]
    assert "답변을 생성 중입니다" in a["chat"] and "절차 reboot의 실패 원인을 분석하고 있습니다" in a["chat"] and a["composer"]["disabled"] is True

