"""큰 작업 요청 안내(D-59): 낱말 규칙으로 큰 요청을 감지해 세션당 한 번 "나눠서 요청"을 권하고, 한도·쓰로틀링으로 중단되면 나눠서 요청하라고 안내한다."""
import pytest
from test_service import REQUEST, build, wait_for
from test_apply import chunks, snapshot  # noqa: F401  (픽스처 재사용)

from app.planner.pipeline import stop_message
from app.session.scale import SPLIT_NOTICE, looks_large

BIG = "<CLUSTER_1> 클러스터를 1.35에서 1.36으로 업그레이드하는 계획을 세워줘"


@pytest.mark.parametrize("text", [
    BIG,
    "eks-main 클러스터 버전 upgrade 계획",
    "노드 그룹을 새 AMI 로 업그레이드해줘",
    "S3 버킷을 다른 계정으로 마이그레이션하는 계획",
])
def test_large_work_is_detected(text):
    assert looks_large(text)


@pytest.mark.parametrize("text", [
    REQUEST,                                                           # 재부팅 한 건
    "보안 그룹에 443 포트를 열어줘",
    "dev-main 클러스터의 버전과 상태를 확인해줘",                              # 클러스터는 있어도 업그레이드가 아니다
    "인스턴스 타입을 업그레이드해줘",                                      # 업그레이드이지만 클러스터·노드 대상이 아니다
    "",
    "<CLUSTER_1> 클러스터 1.35→1.36 업그레이드 중 control plane 변경 전까지의 사전 작업만 계획해줘",      # 이미 나눈 요청
    "업그레이드 사후 작업을 계획해줘 클러스터",
])
def test_small_or_already_split_requests_are_not_flagged(text):
    assert not looks_large(text)


def notices(box, sid):
    return [m for m in box.svc._thread(sid) if m.get("kind") == "notice"]


def test_a_large_request_gets_one_split_notice_per_session_and_still_runs(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot, statuses=["stopped", "plan"])
    c = box.svc.create("alice", "업그레이드", BIG)
    sid = c["session_id"]
    box.svc.join(sid, c["invite"], "bob")
    box.svc.start_generation(sid, BIG)                                                 # 웹 계층이 부르는 생성 시작 경로
    assert len(notices(box, sid)) == 1 and notices(box, sid)[0]["text"] == SPLIT_NOTICE and notices(box, sid)[0]["role"] == "assistant"
    assert wait_for(lambda: box.svc.view(sid, "alice")["pipeline"]["status"] == "stopped")        # 요청은 막히지 않고 계획 생성까지 진행된다
    assert box.svc.handle(sid, "alice", {"type": "retry_generation"})["ok"]            # 다시 시도해도 안내를 되풀이하지 않는다
    assert wait_for(lambda: box.svc.view(sid, "alice")["pipeline"]["status"] == "plan")
    assert len(notices(box, sid)) == 1
    assert SPLIT_NOTICE not in box.pipelines[-1].seen[0] and "나눠서" not in box.pipelines[-1].seen[0]          # 안내문은 모델로 가지 않는다
    box.store.close(); box.maps.close()


def test_a_small_request_gets_no_notice(tmp_path, chunks, snapshot):
    box = build(tmp_path, chunks, snapshot)
    c = box.svc.create("alice", "재부팅", REQUEST)
    box.svc.start_generation(c["session_id"], REQUEST)
    assert wait_for(lambda: box.svc.view(c["session_id"], "alice")["pipeline"]["status"] == "plan")
    assert notices(box, c["session_id"]) == []
    box.store.close(); box.maps.close()


@pytest.mark.parametrize("code", ["tool_calls_exceeded", "steps_exceeded", "tokens_exceeded", "time_exceeded", "output_truncated", "plan_too_large", "llm_unavailable"])
def test_limit_and_throttling_stops_tell_the_user_to_split_the_request(code):
    msg = stop_message(code)
    assert "나눠서" in msg and "사전 작업 / 사후 작업" in msg


def test_other_stops_keep_their_own_guidance():
    assert "나눠서" in stop_message("repair_exhausted") and "구체적으로" in stop_message("repair_exhausted")
    assert "나눠서" not in stop_message("repeated_call") and "나눠서" not in stop_message("leak_blocked")
    assert "(원인: MAIN 호출 실패" in stop_message("llm_unavailable", ("MAIN 호출 실패: ThrottlingException",))            # 원인 종류 덧붙이기는 그대로
