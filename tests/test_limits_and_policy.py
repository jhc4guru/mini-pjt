import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.guards.agent_limits import (
    MAX_REPEAT_NOTICES, AgentBudget, BudgetExceeded, Limits, PlanTooLarge, RepeatedCall, SchemaViolation, ToolNotAllowed, check_plan_size, validate_structured,
)
from app.guards.audit import AuditLog
from app.guards.boundary import LeakError, assert_outbound_clean
from app.guards.command_guard import CommandVerdict
from app.guards.execution_policy import ExecutionDenied, assert_read_only_tool_call, assert_server_executable
from app.guards.input_guard import check_user_input
from app.guards.masking import Inventory, Masker

FIXTURE = Path(__file__).parent / "fixtures" / "inventory.sample.yaml"
GUARDS_DIR = Path(__file__).resolve().parent.parent / "app" / "guards"


def verdict(**kw):
    base = dict(allow=True, executable=True, command="x", kind="read", risk="low", grounded=True)
    base.update(kw)
    return CommandVerdict(**base)


# ---------------- 에이전트 예산 ----------------
def test_only_read_only_tools_are_allowed():
    b = AgentBudget()
    for ok in ("search_docs", "lookup_inventory", "run_read_command"):
        b.before_tool(ok, {"q": ok})
    for bad in ("run_command", "apply", "exec", "write_file", "approve", "delete_resource"):
        with pytest.raises(ToolNotAllowed):
            b.before_tool(bad, {})


def test_tool_call_cap_stops_the_agent():
    b = AgentBudget(Limits(max_tool_calls=3))
    for i in range(3):
        b.before_tool("search_docs", {"q": i})
    with pytest.raises(BudgetExceeded) as e:
        b.before_tool("search_docs", {"q": 99})
    assert e.value.code == "tool_calls_exceeded"


def test_repeated_identical_calls_are_detected_as_a_loop():
    b = AgentBudget(Limits(max_repeats=2))
    b.before_tool("search_docs", {"q": "같은 질의"})
    b.before_tool("search_docs", {"q": "같은 질의"})
    for _ in range(MAX_REPEAT_NOTICES):                         # 처음 몇 번은 중단하지 않고 알린다(D-45)
        with pytest.raises(RepeatedCall) as n:
            b.before_tool("search_docs", {"q": "같은 질의"})
        assert n.value.code == "repeated_call" and n.value.detail == "search_docs" and not isinstance(n.value, BudgetExceeded)
    with pytest.raises(BudgetExceeded) as e:                    # 알림 횟수를 넘으면 중단한다
        b.before_tool("search_docs", {"q": "같은 질의"})
    assert e.value.code == "repeated_call" and e.value.detail == "search_docs"
    b.before_tool("search_docs", {"q": "다른 질의"})          # 인자가 다르면 반복이 아니다


def test_repeat_notices_still_count_against_the_total_call_limit():
    """알림도 호출 수에 들어가므로 같은 호출을 되풀이해도 총 호출 한도에서 반드시 멈춘다."""
    b = AgentBudget(Limits(max_repeats=1, max_tool_calls=4))
    b.before_tool("search_docs", {"q": "x"})
    with pytest.raises(RepeatedCall):
        b.before_tool("search_docs", {"q": "x"})
    with pytest.raises(RepeatedCall):
        b.before_tool("search_docs", {"q": "x"})
    with pytest.raises(BudgetExceeded) as e:
        b.before_tool("search_docs", {"q": "x"})
    assert e.value.code in ("tool_calls_exceeded", "repeated_call")


def test_step_token_and_time_caps():
    with pytest.raises(BudgetExceeded):
        b = AgentBudget(Limits(max_steps=2))
        for _ in range(3):
            b.before_step()
    with pytest.raises(BudgetExceeded):
        AgentBudget(Limits(max_tokens=100)).add_tokens(101)
    now = [0.0]
    b = AgentBudget(Limits(max_seconds=10), clock=lambda: now[0])
    b.before_step()
    now[0] = 11.0
    with pytest.raises(BudgetExceeded) as e:
        b.before_tool("search_docs", {})
    assert e.value.code == "time_exceeded"


def test_exceeding_a_cap_is_a_stop_not_a_continue():
    b = AgentBudget(Limits(max_tool_calls=1))
    b.before_tool("search_docs", {"q": 1})
    for _ in range(3):                                         # 한 번 넘으면 계속 막혀 있다
        with pytest.raises(BudgetExceeded):
            b.before_tool("search_docs", {"q": 2})


def test_plan_size_caps():
    check_plan_size([10, 10, 10])
    with pytest.raises(PlanTooLarge):
        check_plan_size([41])                                  # phase 상한
    with pytest.raises(PlanTooLarge):
        check_plan_size([40, 40, 40, 1])                       # plan 상한


class Step(BaseModel):
    cmd: str
    risk: str


def test_structured_output_is_retried_then_escalated():
    outs = iter(['{"cmd": 1}', "not json", '{"cmd": "x", "risk": "low"}'])
    assert validate_structured(lambda: next(outs), Step, max_retries=2).cmd == "x"
    bad = iter(['{"cmd": 1}'] * 5)
    with pytest.raises(SchemaViolation):
        validate_structured(lambda: next(bad), Step, max_retries=2)


# ---------------- 실행 정책(서버 실행의 단일 관문) ----------------
def test_blocked_and_manual_are_never_executed_even_with_every_approval():
    everything = {"fallback_approval", "strong_warning", "open_cidr_confirm", "dry_run_first", "prod_warning", "show_file_contents"}
    with pytest.raises(ExecutionDenied) as e:
        assert_server_executable(verdict(allow=False, executable=False), approved_by_other=True, confirmations=everything)
    assert e.value.code == "blocked_by_guard"
    with pytest.raises(ExecutionDenied) as e:
        assert_server_executable(verdict(kind="manual", executable=False), approved_by_other=True, confirmations=everything)
    assert "manual" in e.value.code
    with pytest.raises(ExecutionDenied):                       # executable을 True로 위조해도 kind가 manual이면 거부
        assert_server_executable(verdict(kind="manual", executable=True), approved_by_other=True, confirmations=everything)


def test_mutating_commands_need_a_different_approver():
    with pytest.raises(ExecutionDenied) as e:
        assert_server_executable(verdict(kind="mutate"))
    assert e.value.code == "four_eyes_approval_required"
    assert_server_executable(verdict(kind="mutate"), approved_by_other=True)
    assert_server_executable(verdict(kind="read"))             # 읽기는 요청자가 바로 실행


def test_required_confirmations_must_all_be_present():
    v = verdict(kind="mutate", requires=("open_cidr_confirm", "strong_warning"))
    with pytest.raises(ExecutionDenied) as e:
        assert_server_executable(v, approved_by_other=True, confirmations={"strong_warning"})
    assert "open_cidr_confirm" in e.value.code
    assert_server_executable(v, approved_by_other=True, confirmations={"strong_warning", "open_cidr_confirm"})


def test_fallback_needs_its_extra_approval():
    v = verdict(kind="mutate", grounded=False, requires=("fallback_approval",))
    with pytest.raises(ExecutionDenied):
        assert_server_executable(v, approved_by_other=True)
    assert_server_executable(v, approved_by_other=True, confirmations={"fallback_approval"})


def test_read_tool_cannot_run_mutations_or_fallbacks():
    assert_read_only_tool_call(verdict(kind="read"))
    for bad in (verdict(kind="mutate"), verdict(kind="manual", executable=False), verdict(allow=False, executable=False),
                verdict(kind="read", grounded=False), verdict(kind="read", requires=("prod_warning",))):
        with pytest.raises(ToolNotAllowed):
            assert_read_only_tool_call(bad)


# ---------------- 외부 경계 누출 ----------------
def test_outbound_text_with_leaks_is_refused_without_echoing_values():
    m = Masker(Inventory.from_file(FIXTURE))
    m.mask("prod-db sg-0123456789abcdef0")
    assert_outbound_clean("<CLUSTER_1> 의 <SG_1> 조회", m, "llm")
    for leak in ("prod-db 조회", "sg-0123456789abcdef0", "password=hunter2", "계정 123456789012"):
        with pytest.raises(LeakError) as e:
            assert_outbound_clean(leak, m, "llm")
        assert "hunter2" not in str(e.value) and "123456789012" not in str(e.value) and "prod-db" not in str(e.value)


# ---------------- 감사 기록 ----------------
def test_audit_log_records_decisions_without_input_text(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite")
    m = Masker(Inventory.from_file(FIXTURE))
    secret_text = "password=hunter2 로 사용자 만들어"
    v = check_user_input(secret_text, m)
    log.record("s1", "input", v.allow, v.reasons, subject=secret_text)
    log.record("s1", "command", True, (), subject="aws ec2 describe-vpcs")
    rows = log.query(session_id="s1")
    assert len(rows) == 2 and rows[1]["allowed"] is False and rows[1]["reasons"] == ["secret_in_input:secret_assignment"]
    assert rows[1]["subject_len"] == len(secret_text) and rows[1]["subject_hash"] is None   # 시크릿 차단 입력은 해시도 없다
    assert len(rows[0]["subject_hash"]) == 12
    raw = (tmp_path / "audit.sqlite").read_bytes()
    assert b"hunter2" not in raw and "만들어".encode("utf-8") not in raw       # 입력 텍스트는 파일 어디에도 없다
    with pytest.raises(ValueError):
        log.record("s1", "unknown_stage", True)
    log.close()


def test_audit_hash_is_keyed_hmac(tmp_path):
    import hashlib
    text = "aws ec2 describe-vpcs"
    plain = hashlib.sha256(text.encode()).hexdigest()[:12]
    a, b = AuditLog(tmp_path / "a.sqlite"), AuditLog(tmp_path / "b.sqlite")
    for log in (a, b):
        log.record("s", "command", True, (), subject=text)
    ha, hb = a.query()[0]["subject_hash"], b.query()[0]["subject_hash"]
    assert ha != plain and hb != plain and ha != hb          # 설치(키)마다 다르다
    a.record("s", "command", True, (), subject=text)
    assert a.query()[0]["subject_hash"] == ha                 # 같은 설치에서는 같은 입력이 연결된다
    assert (tmp_path / "a.sqlite.key").exists()
    a.close(); b.close()


# ---------------- 우회 경로가 없는지 정적으로 확인 ----------------
def test_guard_modules_read_no_environment_toggles():
    """가드는 환경변수로 끄거나 바꿀 수 없다. (cli_snapshot은 자식 프로세스 환경을 정리하려고 os.environ을 읽는다.)"""
    for path in GUARDS_DIR.glob("*.py"):
        if path.name in ("cli_snapshot.py",):
            continue
        src = path.read_text(encoding="utf-8")
        assert "os.environ" not in src and "getenv" not in src, path.name


def test_no_function_has_a_bypass_parameter():
    import ast

    banned = {"skip", "bypass", "disable", "disabled", "enabled", "unsafe", "force", "off", "no_guard", "strict"}
    for path in GUARDS_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = {a.arg for a in [*node.args.args, *node.args.kwonlyargs]}
                assert not (names & banned), (path.name, node.name, names & banned)


def test_agent_limit_defaults_are_pinned():
    """기본 한도를 바꾸려면 사용자 승인과 D-26 갱신이 필요하다. 값이 조용히 바뀌지 않게 고정한다."""
    lim = Limits()
    assert (lim.max_tool_calls, lim.max_steps, lim.max_tokens, lim.max_seconds, lim.max_repeats) == (30, 40, 700_000, 600.0, 2)
