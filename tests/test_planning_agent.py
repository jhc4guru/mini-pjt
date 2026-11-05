"""계획 에이전트: 도구 호출과 제출 통로, 단계별 검증·수정, 중단 조건, 시스템 프롬프트와 가드의 일치."""
import copy
import json
from pathlib import Path

import pytest

from app.agents.tools import ReadToolbox
from app.guards.agent_limits import READ_ONLY_TOOLS, AgentBudget, Limits
from app.guards.audit import AuditLog
from app.guards.cli_snapshot import CliSnapshot, CliUnavailable
from app.guards.command_guard import AWS_FORBIDDEN_OPTS, KUBECTL_FORBIDDEN_OPTS, TIMEOUT_REQUIRED, CommandGuard, CommandIndex
from app.guards.masking import Inventory, Masker
from app.guards.patterns import find_secrets, find_sensitive
from app.llm import LLMClient
from app.planner.agent import MAX_OUTPUT_TOKENS, PlanningAgent
from app.planner.intake import IntakeResult
from app.planner.prompts import SUBMIT_TOOLS, SYSTEM_PROMPT, build_system_prompt
from app.planner.schema import MAX_PHASE_STEPS
from app.rag.chunks import load_chunks
from app.rag.retriever import HybridRetriever

FIXTURE = Path(__file__).parent / "fixtures" / "inventory.sample.yaml"
INSTANCE = "i-0abc123def4567890"
PH = "<INSTANCE_1>"          # 마스커가 위 인스턴스에 붙이는 플레이스홀더


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeBedrock:
    """미리 정한 응답을 차례로 돌려주고 받은 요청을 기록한다."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def converse(self, **kw):
        self.calls.append(copy.deepcopy(kw))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        content, stop = item
        return {"output": {"message": {"content": content}}, "stopReason": stop, "usage": {"inputTokens": 10, "outputTokens": 10}}


def use(name, inp, i="t1"):
    return {"toolUse": {"toolUseId": i, "name": name, "input": inp}}


def tools(*blocks):
    return (list(blocks), "tool_use")


class FakeRunner:
    def __init__(self):
        self.calls = []

    def run(self, command, *, timeout):
        self.calls.append(command)
        return "ok"


@pytest.fixture(scope="module")
def chunks():
    return load_chunks()


@pytest.fixture(scope="module")
def snapshot(chunks):
    snap = CliSnapshot()
    try:
        snap.build([(c.cli, c.service, c.name) for c in chunks], only_missing=True)
    except CliUnavailable:
        pytest.skip("aws/kubectl이 필요합니다")
    return snap


@pytest.fixture(autouse=True)
def models(monkeypatch):
    for r in ("MAIN", "SUB", "READ"):
        monkeypatch.setenv(f"{r}_MODEL", f"m-{r.lower()}")


@pytest.fixture()
def world(chunks, snapshot, tmp_path):
    masker = Masker(Inventory.from_file(FIXTURE))
    ph = masker.mask(INSTANCE)
    assert ph == PH
    intake = IntakeResult("proceed", masked_text=f"{ph} 재부팅해줘", normalized_query="인스턴스 재부팅", services=("ec2",), intent="change")
    runner, audit = FakeRunner(), AuditLog(tmp_path / "a.sqlite")

    def make(script, limits=None, **kw):
        fake = FakeBedrock(script)
        budget = AgentBudget(limits)
        llm = LLMClient(masker, client=fake, budget=budget, audit=audit, sleep=lambda s: None)
        guard = CommandGuard(CommandIndex(chunks), snapshot, workspace=tmp_path)
        tb = ReadToolbox(masker=masker, guard=guard, retriever=HybridRetriever(chunks, vector_index=None), runner=runner,
                         budget=budget, audit=audit, session_id="s1", search_kwargs={"method": "bm25"})
        return PlanningAgent(llm, tb, guard, masker, budget, audit=audit, **kw), fake, PH

    return make, intake, runner, audit


def outline(*ids):
    return {"summary": "인스턴스 재부팅 계획", "phases": [{"id": i, "title": f"phase {i}", "rollback": "재시작으로 복구"} for i in ids]}


def reboot_steps(ph, *, with_check=True, prefix=""):
    steps = [
        {"id": f"{prefix}look", "kind": "cli", "title": "인스턴스 조회", "command": f"aws ec2 describe-instances --instance-ids {ph}"},
        {"id": f"{prefix}reboot", "kind": "cli", "title": "재부팅", "command": f"aws ec2 reboot-instances --instance-ids {ph}", "depends_on": [f"{prefix}look"]},
    ]
    if with_check:
        steps.append({"id": f"{prefix}verify", "kind": "cli", "title": "상태 확인", "check_for": f"{prefix}reboot",
                      "command": f"aws ec2 describe-instance-status --instance-ids {ph}"})
    return {"steps": steps}


def last_user_blocks(call):
    return call["messages"][-1]["content"]


def test_two_phase_plan_with_research_and_per_phase_submission(world):
    make, intake, runner, _ = world
    agent, fake, _ = make([
        tools(use("search_docs", {"query": "인스턴스 재부팅", "k": 3}, "a")),
        tools(use("submit_outline", outline("p1", "p2"), "b")),
        tools(use("submit_phase", reboot_steps(PH, prefix="a-"), "c")),
        tools(use("submit_phase", {"steps": [{"id": "post", "kind": "check", "title": "서비스 확인", "checklist": ["애플리케이션 동작 확인"]}]}, "d")),
    ])
    r = agent.run(intake, session_id="s1")
    assert r.status == "plan" and [p.id for p in r.plan.phases] == ["p1", "p2"] and r.repairs == 0
    assert r.roles == ("MAIN->MAIN",) * 4
    # 모든 호출에 같은 시스템 프롬프트, 읽기 도구 3개 + 제출 통로 3개, 출력 한도 8K 이상
    for c in fake.calls:
        assert c["system"][0]["text"] == SYSTEM_PROMPT and c["inferenceConfig"]["maxTokens"] == MAX_OUTPUT_TOKENS >= 8192
        assert {t["toolSpec"]["name"] for t in c["toolConfig"]["tools"]} == READ_ONLY_TOOLS | set(SUBMIT_TOOLS)
    # 대화는 user/assistant가 번갈아 나오고, 도구 결과는 직후 user 메시지로 돌아간다
    for c in fake.calls:
        assert [m["role"] for m in c["messages"]] == ["user", "assistant"] * (len(c["messages"]) // 2) + (["user"] if len(c["messages"]) % 2 else [])
    blocks = last_user_blocks(fake.calls[1])
    assert blocks[0]["toolResult"]["toolUseId"] == "a" and "<untrusted_data" in json.dumps(blocks[0], ensure_ascii=False)
    phase2 = last_user_blocks(fake.calls[3])
    assert phase2[0]["toolResult"]["toolUseId"] == "c" and "a-look" in phase2[-1]["text"] and "p2" in phase2[-1]["text"]
    assert runner.calls == []                                                    # 계획 단계에서 변경은 실행되지 않는다


def test_invalid_phase_is_returned_with_codes_and_can_be_repaired(world):
    make, intake, _, _ = world
    agent, fake, _ = make([
        tools(use("submit_outline", outline("p1"), "a")),
        tools(use("submit_phase", reboot_steps("<INSTANCE_1>", with_check=False), "b")),
        tools(use("submit_phase", reboot_steps("<INSTANCE_1>"), "c")),
    ])
    r = agent.run(intake)
    assert r.status == "plan" and r.repairs == 1
    err = last_user_blocks(fake.calls[2])[0]["toolResult"]
    assert err["status"] == "error" and "missing_check:reboot" in json.dumps(err, ensure_ascii=False)


def test_repair_budget_is_finite(world):
    make, intake, _, _ = world
    bad = lambda i: tools(use("submit_phase", reboot_steps("<INSTANCE_1>", with_check=False), i))   # noqa: E731
    agent, _, _ = make([tools(use("submit_outline", outline("p1"), "a")), bad("b"), bad("c"), bad("d")])
    r = agent.run(intake)
    assert r.status == "stopped" and r.reasons == ("repair_exhausted",) and r.plan is None


def test_clarification_ends_the_run_with_questions(world):
    make, intake, _, _ = world
    agent, _, _ = make([tools(use("request_clarification", {"questions": [{"param": "instance", "question": "어떤 인스턴스인가요?"}]}))])
    r = agent.run(intake)
    assert r.status == "clarify" and r.questions == ({"param": "instance", "question": "어떤 인스턴스인가요?"},)


@pytest.mark.parametrize("bad_tool", ["run_command", "apply_plan", "execute", "kubectl", "run_mutating_command"])
def test_unknown_tools_are_refused_and_never_executed(world, bad_tool):
    make, intake, runner, _ = world
    agent, fake, _ = make([
        tools(use(bad_tool, {"command": "aws ec2 terminate-instances --instance-ids <INSTANCE_1>"}, "a")),
        tools(use("request_clarification", {"questions": [{"param": "x", "question": "q?"}]}, "b")),
    ])
    r = agent.run(intake)
    res = last_user_blocks(fake.calls[1])[0]["toolResult"]
    assert res["status"] == "error" and "unknown_tool" in json.dumps(res) and runner.calls == [] and r.repairs == 1


def test_mutating_command_through_the_read_tool_is_not_executed_and_the_agent_goes_on(world):
    make, intake, runner, _ = world
    agent, fake, _ = make([
        tools(use("run_read_command", {"command": f"aws ec2 reboot-instances --instance-ids {'<INSTANCE_1>'}"}, "a")),
        tools(use("request_clarification", {"questions": [{"param": "x", "question": "q?"}]}, "b")),
    ])
    r = agent.run(intake)
    res = last_user_blocks(fake.calls[1])[0]["toolResult"]
    assert res["status"] == "error" and runner.calls == [] and r.status == "clarify"


def test_submit_tool_at_the_wrong_stage_is_refused(world):
    make, intake, _, _ = world
    agent, fake, _ = make([
        tools(use("submit_phase", reboot_steps("<INSTANCE_1>"), "a")),
        tools(use("request_clarification", {"questions": [{"param": "x", "question": "q?"}]}, "b")),
    ])
    agent.run(intake)
    assert "wrong_stage" in json.dumps(last_user_blocks(fake.calls[1])[0], ensure_ascii=False)


def test_plain_text_answers_are_nudged_then_stopped(world):
    make, intake, _, _ = world
    text = (["여기 계획입니다 ..."], "end_turn")
    agent, fake, _ = make([text, text, text])
    r = agent.run(intake)
    assert r.status == "stopped" and r.reasons == ("no_submission",) and len(fake.calls) == 3
    assert "평문 답변" in last_user_blocks(fake.calls[1])[0]["text"]
    agent2, _, _ = make([text, tools(use("request_clarification", {"questions": [{"param": "x", "question": "q?"}]}))])
    assert agent2.run(intake).status == "clarify"


def test_real_values_in_tool_input_are_sent_back_as_errors_and_never_kept_or_resent(world):
    """D-45: 도구 호출 인자의 누출은 실행하지 않고 오류로 돌려 고치게 한다. 누출된 입력은 어떤 요청에도 실리지 않는다. 계속되면 중단한다."""
    make, intake, _, _ = world
    leaky = reboot_steps("<INSTANCE_1>")
    leaky["steps"][0]["command"] = f"aws ec2 describe-instances --instance-ids {INSTANCE}"
    agent, fake, _ = make([tools(use("submit_outline", outline("p1"), "a")), tools(use("submit_phase", leaky, "b")), tools(use("submit_phase", leaky, "c")),
                           tools(use("submit_phase", leaky, "d"))])
    r = agent.run(intake)
    assert r.status == "stopped" and r.reasons == ("model_output_unsafe",) and len(fake.calls) == 4         # 두 번은 고칠 기회, 세 번째에 중단
    assert INSTANCE not in json.dumps(fake.calls)                                                         # 누출된 입력은 어떤 요청에도 실리지 않는다
    errs = [b for m in fake.calls[2]["messages"] for b in m["content"] if "toolResult" in b]
    assert errs and "tool_input_contains_sensitive_value" in json.dumps(errs, ensure_ascii=False)


def test_the_model_can_fix_a_tool_input_that_looked_sensitive_and_the_plan_goes_on(world):
    make, intake, _, _ = world
    leaky = reboot_steps("<INSTANCE_1>")
    leaky["steps"][0]["command"] = f"aws ec2 describe-instances --instance-ids {INSTANCE}"
    agent, fake, _ = make([tools(use("submit_outline", outline("p1"), "a")), tools(use("submit_phase", leaky, "b")),
                           tools(use("submit_phase", reboot_steps("<INSTANCE_1>"), "c"))])
    r = agent.run(intake)
    assert r.status == "plan" and r.plan is not None and INSTANCE not in json.dumps(fake.calls)
    assert any("tool_input_unsafe" in x for x in r.repair_log)


def test_real_values_in_free_text_still_stop_at_once(world):
    make, intake, _, _ = world
    agent, fake, _ = make([([{"text": f"인스턴스 {INSTANCE} 를 재부팅하겠다"}], "end_turn")])
    r = agent.run(intake)
    assert r.status == "stopped" and r.reasons == ("model_output_unsafe",) and len(fake.calls) == 1        # 글(도구 입력이 아닌)의 누출은 바로 중단


def test_truncated_output_is_dropped_and_retried_with_a_shorter_request(world):
    make, intake, _, _ = world
    agent, fake, _ = make([
        ([use("submit_outline", {"summary": "잘림"})], "max_tokens"),
        tools(use("request_clarification", {"questions": [{"param": "x", "question": "q?"}]})),
    ])
    r = agent.run(intake)
    assert r.status == "clarify" and r.repairs == 1
    assert [m["role"] for m in fake.calls[1]["messages"]] == ["user"] and "잘렸다" in fake.calls[1]["messages"][0]["content"][-1]["text"]


def test_budget_limits_stop_the_agent_without_raising(world):
    make, intake, _, _ = world
    agent, _, _ = make([tools(use("search_docs", {"query": "재부팅"}, "a")), tools(use("search_docs", {"query": "중지"}, "b"))], limits=Limits(max_steps=1))
    r = agent.run(intake)
    assert r.status == "stopped" and r.reasons == ("steps_exceeded",)
    agent, _, _ = make([tools(use("search_docs", {"query": "재부팅"}, str(i))) for i in range(5)], limits=Limits(max_repeats=2))
    assert agent.run(intake).reasons == ("repeated_call",)


def test_main_throttling_falls_back_to_sub_and_the_plan_still_builds(world):
    make, intake, _, audit = world
    t = ClientError("ThrottlingException")
    agent, fake, _ = make([t, t, t, tools(use("submit_outline", outline("p1"), "a")), tools(use("submit_phase", reboot_steps("<INSTANCE_1>"), "b"))])
    r = agent.run(intake, session_id="s1")
    assert r.status == "plan" and r.roles[0] == "MAIN->SUB" and r.roles[1] == "MAIN->MAIN"
    assert [c["modelId"] for c in fake.calls][:4] == ["m-main"] * 3 + ["m-sub"]


def test_agent_refuses_requests_that_intake_did_not_clear(world):
    make, _, _, _ = world
    agent, _, _ = make([])
    with pytest.raises(ValueError):
        agent.run(IntakeResult("clarify"))


def test_stops_are_audited_without_input_text(world):
    make, intake, _, audit = world
    agent, _, _ = make([(["평문"], "end_turn")] * 3)
    agent.run(intake, session_id="s1")
    rows = audit.query(session_id="s1", stage="limits")
    assert rows[0]["reasons"] == ["agent_stopped:no_submission"] and "재부팅" not in str(rows)


# ---------------- 시스템 프롬프트 ----------------
def test_system_prompt_mirrors_the_guard_constants_and_tool_names():
    p = SYSTEM_PROMPT
    for opt in AWS_FORBIDDEN_OPTS | KUBECTL_FORBIDDEN_OPTS:
        assert f"`{opt}`" in p, opt
    for cmd in TIMEOUT_REQUIRED:
        assert f"`{cmd}`" in p and "--timeout" in p
    for tool in READ_ONLY_TOOLS | set(SUBMIT_TOOLS):
        assert f"`{tool}`" in p, tool
    for kind in ("cli", "manual", "iac", "check", "poll"):
        assert f"`{kind}`" in p
    assert str(MAX_PHASE_STEPS) in p and "120" in p and "30회" in p and "40회" in p
    custom = build_system_prompt(Limits(max_tool_calls=7, max_steps=9))
    assert "7회" in custom and "9회" in custom


def test_system_prompt_lists_every_manual_command_in_the_docs(chunks):
    for c in chunks:
        if c.kind == "manual":
            assert (c.name if c.cli == "aws" else c.name.split()[0]) in SYSTEM_PROMPT, c.id


def test_system_prompt_has_no_sensitive_values_and_states_the_core_rules():
    assert not find_sensitive(SYSTEM_PROMPT) and not find_secrets(SYSTEM_PROMPT)
    for phrase in ("마스킹", "플레이스홀더", "untrusted_data", "EC2 인스턴스", "Pod은 Pod", "문서 근거 없음", "확인 step",
                   "게이트", "롤백", "우회하지 않는다", "제안만", "iac_managed", "request_clarification"):
        assert phrase in SYSTEM_PROMPT, phrase


def test_system_prompt_prefers_kubectl_over_aws_eks_for_status_checks():
    """사용자 요청: aws eks는 잘 쓰지 않으니 상태 확인·대기는 kubectl을 우선한다."""
    for phrase in ("도구 선택(중요)", "aws eks`를 거의 쓰지 않는다", "kubectl get nodes -o wide", "kubectl rollout status daemonset/kube-proxy", "5분의 1", "aws autoscaling"):
        assert phrase in SYSTEM_PROMPT, phrase


def test_accepted_phase_compacts_the_conversation_so_input_does_not_keep_growing(world):
    make, intake, _, _ = world
    post = {"steps": [{"id": "post", "kind": "check", "title": "서비스 확인", "checklist": ["동작 확인"]}]}
    agent, fake, _ = make([
        tools(use("submit_outline", outline("p1", "p2"), "o")),
        tools(use("search_docs", {"query": "인스턴스 재부팅"}, "s")),                     # phase 1: 조사
        tools(use("submit_phase", reboot_steps(PH, with_check=False), "bad")),             # phase 1: 실패한 제출
        tools(use("submit_phase", reboot_steps(PH, prefix="a-"), "good")),                 # phase 1: 수락
        tools(use("submit_phase", post, "p2")),
    ])
    r = agent.run(intake)
    assert r.status == "plan" and r.repairs == 1
    last = fake.calls[-1]["messages"]                                     # phase 2를 요청하는 호출
    dumped = json.dumps(last, ensure_ascii=False)
    assert "missing_check" not in dumped and "untrusted_data" not in dumped and '"bad"' not in dumped and '"s"' not in dumped
    kept = [b["toolUse"] for m in last if m["role"] == "assistant" for b in m["content"] if "toolUse" in b and b["toolUse"]["name"] == "submit_phase"]
    assert kept and kept[0]["input"] == {"accepted_steps": 3}
    assert "a-look" in last[-1]["content"][-1]["text"] and "a-reboot" in last[-1]["content"][-1]["text"]      # 앞선 phase는 요약으로 전달
    assert [m["role"] for m in last] == ["user", "assistant", "user", "assistant", "user"]
    assert len(r.token_log) == 5 and r.repair_log and r.repair_log[0].startswith("submit_phase:missing_check")


def test_outline_research_is_dropped_after_acceptance_and_replaced_by_summaries(world):
    make, intake, _, _ = world
    agent, fake, _ = make([
        tools(use("search_docs", {"query": "인스턴스 재부팅", "k": 3}, "s")),
        tools(use("submit_outline", outline("p1", "p2"), "o")),
        tools(use("submit_phase", reboot_steps(PH, prefix="a-"), "p1")),
        tools(use("submit_phase", {"steps": [{"id": "post", "kind": "check", "title": "서비스 확인", "checklist": ["동작 확인"]}]}, "p2")),
    ])
    r = agent.run(intake)
    assert r.status == "plan"
    first_phase_call = json.dumps(fake.calls[2]["messages"], ensure_ascii=False)                  # phase 1을 요청하는 호출
    assert "untrusted_data" not in first_phase_call and '"id": "s"' not in first_phase_call and '"toolUseId": "s"' not in first_phase_call
    text = fake.calls[2]["messages"][-1]["content"][-1]["text"]
    assert "수락된 계획 윤곽" in text and "p1:" in text and "p2:" in text                  # 윤곽 요약
    assert "aws ec2 reboot-instances" in text and "(mutate)" in text                    # 찾은 문서 명령 요약
    assert [m["role"] for m in fake.calls[2]["messages"]] == ["user", "assistant", "user"]


def test_system_prompt_steers_ami_lookup_to_public_ssm_parameters_not_account_ids():
    """실측: 모델이 `describe-images --owners 099…`(제작사 공개 계정 ID)를 써서 계정 ID 패턴에 걸려 계획 생성이 멈췄다."""
    for phrase in ("AMI 찾기", "12자리 숫자(계정 ID)", "SSM 공개 파라미터", "/aws/service/canonical/ubuntu", "ami-amazon-linux-latest"):
        assert phrase in SYSTEM_PROMPT, phrase
    assert not find_sensitive(SYSTEM_PROMPT) and not find_secrets(SYSTEM_PROMPT)


def test_system_prompt_tells_the_agent_to_use_previous_questions_and_treat_default_as_an_answer():
    """실사용에서 발견: "기본으로"라고 답했는데 같은 질문을 되풀이했다."""
    for phrase in ("[이전 질문]", "[추가 정보]", "이미 답한 것은 다시 묻지 않는다", "기본(default) VPC·서브넷·보안 그룹", "isDefault,Values=true", "default-for-az", "\"없음\"이라고 한 선택 항목"):
        assert phrase in SYSTEM_PROMPT, phrase
    assert not find_sensitive(SYSTEM_PROMPT) and not find_secrets(SYSTEM_PROMPT)
