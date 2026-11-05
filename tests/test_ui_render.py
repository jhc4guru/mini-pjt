"""화면 렌더링: 실제 서비스가 만든 스냅샷을 가짜 DOM(node)에서 그려 예외 없이 그려지고, 3분할(이력·절차·대화)의 내용, 역할·상태에 맞는 버튼,
드래그앤드롭 순서 변경 메시지를 확인한다.

실제 브라우저가 아니라 `tests/ui_harness.js`의 가짜 DOM이다(레이아웃·스타일은 확인하지 않는다). 실제 Chrome 확인은 `tests/browser/`(수동).
"""
import json
import os
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
             snap(box, sid, "insert_after", "alice", clicks=["＋"])]
    analyzing = snap(box, sid, "analyzing_alice", "alice")
    analyzing["view"]["analyzing"] = "reboot"
    cases.append(analyzing)
    h = svc.view(sid, "alice")["plan_hash"]
    svc.handle(sid, "bob", {"type": "approve_plan", "version": 1, "hash": h})
    cases.append(snap(box, sid, "approved", "alice", click="apply 진행 →", ctab="apply"))
    svc.handle(sid, "alice", {"type": "start_apply"})
    cases.append(snap(box, sid, "applying_pending_alice", "alice", sel="reboot", ctab="apply"))
    cases.append(snap(box, sid, "applying_pending_bob", "bob", ctab="apply"))
    for who in ("alice", "bob"):                                                      # 전체 개방 + 강한 경고가 걸린 변경 절차: 누가 무엇을 해야 하는지
        tv = snap(box, sid, f"todo_{who}", who, sel="reboot", ctab="apply")
        for p in tv["view"]["plan"]["phases"]:
            for st in p["steps"]:
                if st["id"] == "reboot":
                    st["requires"] = ["strong_warning", "open_cidr_confirm"]
        tv["view"]["pending_requirements"] = {"reboot": ["strong_warning", "open_cidr_confirm"]}
        cases.append(tv)
    pre = snap(box, sid, "preapproved_bob", "bob", ctab="apply")
    pre["view"]["preapproved"], pre["view"]["preapprovable"] = ["reboot"], ["reboot"]
    cases.append(pre)
    bl = snap(box, sid, "blocked_todo_alice", "alice", ctab="apply")
    for p in bl["view"]["plan"]["phases"]:
        for st in p["steps"]:
            if st["id"] == "reboot":
                st["depends_on"] = ["verify"]                                              # 앞 절차(verify)가 끝나기 전
    cases.append(bl)
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
    # 빈 plan(연습장) 모드: 초안 한 개(확정 전) → 확정 가능한 것 확정
    (tmp_path / "b5").mkdir()
    box4 = build(tmp_path / "b5", chunks, snapshot)
    e = box4.svc.create("alice", "빈 연습장")
    esid = e["session_id"]
    cases.append(snap(box4, esid, "empty_alice", "alice"))
    cases.append(snap(box4, esid, "empty_builder", "alice", bld={"open": True, "query": "", "results": [], "picked": None, "vals": {}, "opts": {}, "extra": "", "title": "", "chk": "", "after": None, "phase": None, "lookups": {}}))
    cases.append(snap(box4, esid, "empty_bob", "bob"))
    cases.append(snap(box4, esid, "echo_alice", "alice", echo={"text": "EC2 만들어줘"}))
    cases.append(snap(box4, esid, "blank_click", "alice", clicks=["＋ 직접 입력"]))
    cases.append(snap(box4, esid, "iac_click", "alice", clicks=["＋ 수동·IaC ▾", "IaC 코드 변경"]))
    r = box4.svc.handle(esid, "alice", {"type": "scratch", "op": "add_step", "phase": None, "step": {"title": "VPC 조회", "kind": "cli", "command": "aws ec2 describe-vpcs"}})
    cases.append(snap(box4, esid, "drafts_alice", "alice", draft_result=r))
    box4.svc.handle(esid, "alice", {"type": "scratch", "op": "add_step", "step": {"kind": "cli", "title": "재부팅", "command": "aws ec2 reboot-instances --instance-ids i-0abc123def4567890"}})
    box4.svc.handle(esid, "alice", {"type": "scratch", "op": "add_step", "step": {"kind": "cli", "title": "빈 절차", "command": ""}})
    blocked = snap(box4, esid, "blocked_alice", "alice")
    mut = next(x for p in blocked["view"]["scratch"]["phases"] for x in p["steps"] if x["title"] == "재부팅")
    blocked["sel"] = mut["id"]
    cases.append(blocked)
    cases.append(snap(box4, esid, "blocked_click", "alice", sel=mut["id"], clicks=["이 변경의 확인(읽기) 절차 만들기"]))
    h4 = lambda step: box4.svc.handle(esid, "alice", {"type": "scratch", "op": "add_step", "step": step})        # noqa: E731
    h4({"id": "mk-sg", "kind": "cli", "title": "SG 생성", "command": "aws ec2 create-security-group --group-name web --description web"})
    h4({"id": "uses", "kind": "cli", "title": "SG 조회", "command": "aws ec2 describe-security-groups --group-ids @{mk-sg.GroupId}"})
    cases.append(snap(box4, esid, "refs_alice", "alice", sel="uses"))
    pv = snap(box4, esid, "proposal_alice", "alice")
    first = pv["view"]["scratch"]["phases"][0]["steps"][0]["id"]
    pv["view"]["proposal"] = {"id": "p1", "by": "bob", "summary": "지움", "status": "open", "ops": [{"op": "delete_step", "step_id": first}, {"op": "add_step", "step": {"kind": "cli", "title": "VPC", "command": "aws ec2 describe-vpcs"}, "after": first}]}
    cases.append(pv)
    pg = snap(box4, esid, "proposing_alice", "alice")
    pg["view"]["proposing"] = True
    cases.append(pg)
    # 반려: 확인자가 코멘트와 함께 돌려보낸 뒤 요청자·확인자의 화면
    (tmp_path / "b6").mkdir()
    box6 = build(tmp_path / "b6", chunks, snapshot)
    r6 = box6.svc.create("alice", "반려 화면", REQUEST)
    s6 = r6["session_id"]
    box6.svc.generate_plan(s6, REQUEST)
    box6.svc.join(s6, r6["invite"], "bob")
    cases.append(snap(box6, s6, "reject_open_bob", "bob", clicks=["반려(수정 요청)"]))
    cases.append(snap(box6, s6, "reject_send_empty_bob", "bob", clicks=["반려(수정 요청)", "반려 보내기"]))
    v6 = box6.svc.view(s6, "bob")
    box6.svc.handle(s6, "bob", {"type": "reject_plan", "version": v6["version"], "hash": v6["plan_hash"], "summary": "대상 범위를 줄이고 확인을 추가해 주세요", "steps": {"reboot": "대상을 하나로 줄이기"}})
    cases.append(snap(box6, s6, "rejected_alice", "alice"))
    cases.append(snap(box6, s6, "rejected_bob", "bob"))
    box6.store.close()
    box6.maps.close()
    f = tmp_path / "cases.json"
    f.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    expr = 'JSON.stringify({a: dedupeHistory(%s, null, null), b: dedupeHistory(%s, "s1", "bob"), c: dedupeHistory(%s, "s2", "alice")})' % ('[{sid:"s1", nick:"alice", requester:"alice", updated_at:5}, {sid:"s1", nick:"bob", requester:"alice", updated_at:5}, {sid:"s2", nick:"bob", requester:"carol", updated_at:9}, {sid:"s2", nick:"alice", requester:"carol", updated_at:9}, {sid:"s3", nick:"x", requester:"x", updated_at:7}]', ('[{sid:"s1", nick:"alice", requester:"alice", updated_at:5}, {sid:"s1", nick:"bob", requester:"alice", updated_at:5}, {sid:"s2", nick:"bob", requester:"carol", updated_at:9}, {sid:"s2", nick:"alice", requester:"carol", updated_at:9}, {sid:"s3", nick:"x", requester:"x", updated_at:7}]'), ('[{sid:"s1", nick:"alice", requester:"alice", updated_at:5}, {sid:"s1", nick:"bob", requester:"alice", updated_at:5}, {sid:"s2", nick:"bob", requester:"carol", updated_at:9}, {sid:"s2", nick:"alice", requester:"carol", updated_at:9}, {sid:"s3", nick:"x", requester:"x", updated_at:7}]'))
    out = subprocess.run([node, str(ROOT / "tests" / "ui_harness.js"), str(ROOT / "app" / "static" / "index.html"), str(f)], capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "UI_EVAL": json.dumps(expr)})
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
    assert btn(rendered, "review_alice", "확정 (일괄 승인)")["disabled"] is True
    assert btn(rendered, "review_bob", "확정 (일괄 승인)")["disabled"] is False
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
    assert rendered["review_alice"]["composer"]["disabled"] is False and "말로 하세요" in rendered["review_alice"]["composer"]["placeholder"]          # 연습장에 절차가 있으면 말로 수정


# ---------------- 드래그앤드롭 ----------------
def test_steps_are_draggable_only_while_the_plan_can_be_edited(rendered):
    assert set(rendered["review_alice"]["draggable"]) == set(rendered["review_alice"]["rows"]) and rendered["review_alice"]["rows"]
    assert rendered["applying_pending_alice"]["draggable"] == [] and rendered["applying_plan_tab"]["draggable"] == []           # apply 중에는 어느 탭에서도 끌 수 없다


def test_dropping_a_step_on_another_sends_a_move_with_the_step_it_should_follow(rendered):
    sent = rendered["drag_move"]["sent"]
    rows = rendered["drag_move"]["rows"]
    assert len(sent) == 1 and sent[0]["type"] == "scratch" and sent[0]["op"] == "move_step" and sent[0]["step_id"] == "peek" and sent[0]["phase"] == "p1"
    target = rows.index("reboot")
    assert sent[0]["after"] == (rows[target - 1] if target > 0 else None)


def test_dropping_on_the_end_zone_and_on_itself(rendered):
    end, rows = rendered["drag_end"]["sent"], rendered["drag_end"]["rows"]
    assert len(end) == 1 and end[0] == {"type": "scratch", "op": "move_step", "step_id": "reboot", "phase": "p1", "after": rows[-1]}
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
    assert "명령 만들기" in r["centerAfter"] and "이 줄 바로 뒤에 들어갑니다" in r["centerAfter"]          # ＋는 그 줄 뒤에 넣을 명령 만들기 도우미를 연다
    assert r["sent"] == []                                                                          # 만들기를 마치기 전에는 서버로 아무것도 보내지 않는다


# ---------------- 대화는 작업별, 분석 중 표시 ----------------
def test_the_conversation_header_names_the_task_and_shows_a_generating_indicator_during_analysis(rendered):
    assert "대화 · EKS 재부팅 작업" in rendered["review_alice"]["chat"] and "대화 · 되묻기 작업" in rendered["clarify_alice"]["chat"]
    a = rendered["analyzing_alice"]
    assert "답변을 생성 중입니다" in a["chat"] and "절차 reboot의 실패 원인을 분석하고 있습니다" in a["chat"] and a["composer"]["disabled"] is True



# ---------------- 연습장(Plan 탭) ----------------
def test_an_empty_plan_can_be_started_from_a_title_and_shows_the_scratchpad_with_the_toolbar(rendered):
    assert "빈 plan으로 시작" in rendered["newtask"]["center"]
    e = rendered["empty_alice"]
    assert e["composer"]["disabled"] is False and "초안을 요청" in e["composer"]["placeholder"] and e["composer"]["buttons"][0]["text"] == "초안 요청"
    texts = [b["text"] for b in e["buttons"]]
    assert "＋ 명령 만들기" in texts and "＋ 직접 입력" in texts and any("검토 요청" in t for t in texts) and not any("aws eks" in t for t in texts)      # 일괄 변환 버튼은 없다
    assert "연습장" in e["center"]
    b = rendered["empty_bob"]["composer"]
    assert b["disabled"] is True                                                                       # 초안 요청은 요청자만


def test_the_command_builder_panel_opens_with_a_search_box(rendered):
    c = rendered["empty_builder"]["center"]
    assert "명령 만들기" in c and "닫기" in [b["text"] for b in rendered["empty_builder"]["buttons"]]


def test_a_draft_shows_a_validation_icon_and_a_confirm_checkbox_and_send_needs_a_confirmed_step(rendered):
    d = rendered["drafts_alice"]
    assert d["rows"] and "VPC 조회" in d["center"]
    send = [b for b in d["buttons"] if "검토 요청" in b["text"]]
    assert send and send[0]["disabled"] is True                                                         # 확정한 절차가 없으면 보낼 수 없다


def test_a_blank_step_can_be_typed_directly_and_the_pad_is_open_even_while_the_agent_asks(rendered):
    r = rendered["blank_click"]
    assert len(r["sent"]) == 1 and r["sent"][0]["type"] == "scratch" and r["sent"][0]["op"] == "add_step"
    assert r["sent"][0]["step"] == {"kind": "cli", "title": "새 절차", "command": ""}                    # 비어 있어도 초안으로 저장된다
    c = rendered["clarify_alice"]
    assert "Plan — 연습장" in c["center"] and any(b["text"] == "＋ 직접 입력" for b in c["buttons"])    # 되묻는 중에도 연습장
    assert "빈 plan으로 시작" in rendered["newtask"]["center"] and "두 가지로 시작" in rendered["newtask"]["center"]


def test_a_sent_message_is_echoed_at_once_with_a_working_notice_and_the_input_is_locked(rendered):
    e = rendered["echo_alice"]
    assert "EC2 만들어줘" in e["chat"] and "요청을 받았습니다. 처리 중입니다" in e["chat"]
    assert e["composer"]["disabled"] is True and "처리 중입니다" in e["composer"]["placeholder"]
    assert "처리 중" not in rendered["empty_alice"]["chat"]                                          # 보내기 전에는 보이지 않는다


def test_the_composer_only_holds_back_server_updates_while_it_has_text(rendered):
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'if ($comp.contains(a)) return !!(a.value || "").trim();' in html                     # 보낸 직후 빈 입력창이 새 상태를 막지 않는다


def test_a_spoken_edit_is_offered_in_the_composer_and_a_proposal_is_shown_as_a_diff_to_accept_or_dismiss(rendered):
    d = rendered["drafts_alice"]["composer"]
    assert [b["text"] for b in d["buttons"]][:2] == ["초안 요청", "말로 요청"]                                  # 연습장에 절차가 있으면 말로 수정
    r = rendered["review_alice"]["composer"]
    assert r["disabled"] is False and "말로 하세요" in r["placeholder"] and [b["text"] for b in r["buttons"]] == ["말로 요청"]
    p = rendered["proposal_alice"]
    assert "수정안 · bob 요청" in p["chat"] and "지우기" in p["chat"] and "추가" in p["chat"] and "aws ec2 describe-vpcs" in p["chat"]
    assert [b["text"] for b in p["chatButtons"]] == ["수락", "무시"]
    assert "말로 요청" not in [b["text"] for b in p["composer"]["buttons"]]                                    # 열린 수정안이 있으면 새로 만들지 않는다
    assert "수정안을 만드는 중입니다" in rendered["proposing_alice"]["chat"] and "연습장은 아직 바뀌지 않았습니다" in rendered["proposing_alice"]["chat"]


def test_a_blocked_draft_says_why_on_its_row_uses_a_stop_sign_not_a_delete_x_and_explains_when_the_checkbox_is_pressed(rendered):
    b = rendered["blocked_alice"]
    assert "검토 요청에 포함하려면: " in b["center"] and "명령이 비어 있습니다" in b["center"]                      # 이유가 줄 아래에 보인다
    assert "⛔" in b["center"] and "✖" not in b["center"].split("실행 로그")[0]                         # 삭제처럼 보이는 ✖를 쓰지 않는다
    assert "삭제가 아닙니다" in b["center"]
    assert "이 변경의 확인(읽기) 절차 만들기" in [x["text"] for x in b["buttons"]]                    # 변경 뒤 확인 절차 바로가기
    c = rendered["blocked_click"]
    assert "↳ ‘명령 만들기’로 만든 절차는 이 줄 바로 뒤에 들어갑니다." in c["centerAfter"] and c["sent"] == []


def test_phase_and_step_names_can_be_edited_in_place(rendered):
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'class:"ptitle"' in html and "더블클릭해서 이름 바꾸기" in html and "renameInline" in html
    assert "convert_eks" not in html and "일괄 변환" not in html.replace("<!--", "")


def test_human_steps_can_be_started_from_the_toolbar_menu(rendered):
    e = rendered["empty_alice"]
    assert "＋ 수동·IaC ▾" in [b["text"] for b in e["buttons"]]
    r = rendered["iac_click"]
    assert len(r["sent"]) == 1 and r["sent"][0]["op"] == "add_step" and r["sent"][0]["step"]["kind"] == "iac" and r["sent"][0]["step"]["checklist"]


def test_a_step_using_an_earlier_result_is_badged_and_offers_the_earlier_ids_to_pick(rendered):
    r = rendered["refs_alice"]
    assert "↩ 앞 결과 사용" in r["center"]                                                            # 줄 배지
    assert "앞 절차 결과 쓰기" in r["center"] and "SG 생성 → 보안 그룹 ID" in r["center"]               # 상세의 선택 목록
    assert "@{mk-sg.GroupId}" in r["center"]                                                        # 명령은 템플릿 그대로 보인다


def test_the_same_task_opened_with_two_nicknames_is_one_history_line(rendered):
    r = json.loads(rendered["eval"])
    ids = lambda items: [(e["sid"], e["nick"]) for e in items]                                          # noqa: E731
    assert ids(r["a"]) == [("s2", "bob"), ("s3", "x"), ("s1", "alice")]                                # 작업마다 한 줄, 닫혀 있으면 요청자 닉네임을 우선(요청자 자격이 없으면 먼저 것)
    assert ("s1", "bob") in ids(r["b"]) and len([e for e in r["b"] if e["sid"] == "s1"]) == 1          # 열려 있는 닉네임이 우선
    assert ("s2", "alice") in ids(r["c"]) and len([e for e in r["c"] if e["sid"] == "s2"]) == 1


def test_the_header_and_tooltip_show_the_task_times(rendered):
    c = rendered["review_alice"]["center"]
    assert "생성 " in c and "Plan 전송 " in c and "마지막 수정 " in c
    assert "Apply 시작 " in rendered["applying_pending_alice"]["center"]


def test_the_screen_words_make_clear_that_the_reviewer_confirms_before_apply(rendered):
    a, b = rendered["review_alice"], rendered["review_bob"]
    assert "확인자 확정(승인) 대기" in a["center"] and "요청자가 아닌 확인자가 확정(승인)해야 apply로 갈 수 있습니다" in a["center"]
    assert btn(rendered, "review_alice", "확정 (일괄 승인)")["disabled"] is True and btn(rendered, "review_bob", "확정 (일괄 승인)")["disabled"] is False
    e = rendered["drafts_alice"]
    assert any(x["text"].startswith("검토 요청 (") for x in e["buttons"]) and "검토 요청에 포함" in e["center"]
    assert not any("Apply로 보내기" in x["text"] for x in e["buttons"])


def test_each_step_and_the_apply_top_say_whose_turn_it_is_and_what_to_do(rendered):
    a, b = rendered["todo_alice"], rendered["todo_bob"]
    assert "내가 지금 할 일" in a["center"] and "상대가 할 일" in a["center"]
    # 요청자: 전체 개방 사유 입력이 내 일, 강한 경고 확인·절차 승인은 확인자 일
    assert "▶ 내 차례: 전체 개방 사유를 입력" in a["center"] and "요청자가 아닌 확인자: 강한 경고 확인" in a["center"]
    # 확인자: 강한 경고 확인이 내 일(전체 개방은 요청자의 사유 뒤)
    assert "▶ 내 차례: 강한 경고 확인" in b["center"] and "요청자: 전체 개방 사유를 입력" in b["center"]
    assert "… 앞 절차가 끝나면 진행합니다" in rendered["blocked_todo_alice"]["center"]                      # 앞 절차가 끝나기 전의 변경 절차는 회색으로 대기 표시
    assert "필요한 조치 — 누가 무엇을 하는지" in a["center"] and "▶ 내가 할 일: 전체 개방 사유를 입력" in a["center"]
    assert "⏳ 요청자가 아닌 확인자의 일: 강한 경고 확인" in a["center"] and "⏳ 요청자의 일: 전체 개방 사유를 입력" in b["center"]
    reads = rendered["applying_pending_alice"]["center"]
    assert "내가 지금 할 일" in reads and "‘확인(실행)’ 버튼을 누르세요" not in rendered["review_alice"]["center"]     # plan 검토 중에는 패널이 없다


def test_the_plan_card_lists_what_is_preapproved_and_what_needs_its_own_approval_and_rows_show_the_preapproval(rendered):
    c = rendered["review_bob"]["center"]
    assert "확정(일괄 승인)하면: " in c and "함께 사전 승인되는 변경 절차가 없습니다" in c
    assert "개별 승인이 필요한 변경 1개: 재부팅(중단 가능성 높음)" in c                                  # 막힌 이유가 이름으로 보인다
    p = rendered["preapproved_bob"]["center"]
    assert "사전 승인" in p and "사전 승인됨 — 곧 ▶ 실행할 수 있습니다" in p


def test_the_reviewer_can_reject_with_comments_and_the_requester_sees_them_on_the_rows(rendered):
    assert "반려(수정 요청)" in [b["text"] for b in rendered["review_bob"]["buttons"]] and "반려(수정 요청)" not in [b["text"] for b in rendered["review_alice"]["buttons"]]
    o = rendered["reject_open_bob"]
    assert "보낸 코멘트는 요청자의 연습장에 표시됩니다" in o["centerAfter"] and "반려 보내기" in o["centerAfter"]      # 버튼을 누르면 코멘트 패널이 열린다
    assert o["sent"] == []
    assert rendered["reject_send_empty_bob"]["sent"] == []                                          # 이유가 비어 있으면 보내지 않는다
    a = rendered["rejected_alice"]["center"]
    assert "반려됨 — bob:" in a and "대상 범위를 줄이고 확인을 추가해 주세요" in a and "💬 bob: 대상을 하나로 줄이기" in a
    assert "반려됨(수정 후 다시 검토 요청)" in a and "반려 " in a and "반려됨 · 수정 중" in a                                      # plan 카드 상태와 시각
    assert [b for b in rendered["rejected_alice"]["buttons"] if b["text"].startswith("검토 요청 (")]   # 고친 뒤 다시 검토를 요청할 수 있다
    assert "확정 (일괄 승인)" not in [b["text"] for b in rendered["rejected_bob"]["buttons"]] and "반려(수정 요청)" not in [b["text"] for b in rendered["rejected_bob"]["buttons"]]
