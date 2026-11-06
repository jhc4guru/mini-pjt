"""로컬 트레이스(T7-1, D-60): 메타데이터만 기록하고, 원본 입력·실제 값·도구 결과가 저장소로 가지 않는다(누출은 fail-closed로 차단)."""
import sqlite3
from pathlib import Path

import pytest
from test_agent_tools import FIXTURE, INSTANCE, SG, FakeRunner, chunks, snapshot  # noqa: F401  (픽스처 재사용)
from test_llm_client import ClientError, FakeBedrock

from app.agents.tools import ReadToolbox
from app.guards.agent_limits import AgentBudget, BudgetExceeded, Limits
from app.guards.command_guard import CommandGuard, CommandIndex
from app.guards.masking import Inventory, Masker
from app.llm import LLMClient
from app.observability import LocalTracer, NullTracer, TraceConfigError, make_tracer
from app.rag.retriever import HybridRetriever


@pytest.fixture(autouse=True)
def models(monkeypatch):
    for r in ("MAIN", "SUB", "READ"):
        monkeypatch.setenv(f"{r}_MODEL", f"m-{r.lower()}")


@pytest.fixture()
def tracer(tmp_path):
    t = LocalTracer(tmp_path / "trace.sqlite")
    yield t
    t.close()


def raw_bytes(tmp_path) -> bytes:
    return b"".join(p.read_bytes() for p in Path(tmp_path).glob("trace.sqlite*"))


def llm(plan, tracer, **kw):
    return LLMClient(Masker(Inventory.from_file(FIXTURE)), client=FakeBedrock(plan), sleep=lambda s: None, tracer=tracer, **kw)


def test_a_model_call_records_only_metadata_never_the_prompt_or_the_answer(tracer, tmp_path):
    prompt, answer = "인스턴스 재부팅 계획을 세워줘 비밀문장", "모델의 답변 본문"
    r = llm({"m-main": [answer]}, tracer).complete("MAIN", prompt, session_id="abc123")
    assert r.text == answer
    (e,) = tracer.events()
    assert (e["kind"], e["role"], e["actual_role"], e["model"], e["ok"], e["session_id"]) == ("llm", "MAIN", "MAIN", "m-main", 1, "abc123")
    assert (e["input_tokens"], e["output_tokens"]) == (10, 5) and e["latency_ms"] >= 0
    data = raw_bytes(tmp_path)
    assert prompt.encode() not in data and answer.encode() not in data and "비밀문장".encode() not in data      # 본문은 어디에도 없다


def test_throttling_and_fallback_show_up_in_the_summary(tracer):
    t = ClientError("ThrottlingException")
    llm({"m-main": [t, t, t], "m-sub": ["from-sub"]}, tracer).complete("MAIN", "x", session_id="s1")
    llm({"m-main": ["ok"]}, tracer).complete("MAIN", "y", session_id="s1")
    s = tracer.summary()
    assert s["llm_calls"] == 2 and s["llm_failures"] == 1 and s["throttles"] == 1
    assert s["fallback_calls"] == 1 and s["fallback_ratio"] == 0.5
    assert s["by_role"]["SUB"]["calls"] == 1 and s["by_role"]["MAIN"]["calls"] == 1 and s["input_tokens"] == 20 and s["output_tokens"] == 10
    assert tracer.summary(session_id="nobody")["llm_calls"] == 0


def test_other_model_errors_record_the_error_kind_only(tracer, tmp_path):
    from app.llm import LLMUnavailable
    with pytest.raises(LLMUnavailable):
        llm({"m-read": [ClientError("AccessDeniedException")]}, tracer).complete("READ", "계정 비밀 문장")
    (e,) = tracer.events()
    assert e["ok"] == 0 and e["code"] == "ClientError:AccessDeniedException" and "계정 비밀 문장".encode() not in raw_bytes(tmp_path)


@pytest.mark.parametrize("field,value", [
    ("name", INSTANCE),                                  # 리소스 ID 형태
    ("code", "AKIAIOSFODNN7EXAMPLE"),                    # 액세스 키 형태
    ("name", "10.20.30.40"),                             # IP
    ("code", "123456789012"),                            # 계정 ID
    ("name", "arn:aws:iam::123456789012:role/x"),        # ARN
    ("name", "사용자가 입력한 원문 문장"),                      # 안전하지 않은 문자(자유 텍스트)
    ("model", "a b c"),
])
def test_leaky_or_free_text_fields_are_blocked_and_never_stored(tracer, tmp_path, field, value):
    tracer.record("tool", **{"name": "search_docs", field: value}) if field != "name" else tracer.record("tool", value)
    (e,) = tracer.events()
    assert e[field] in ("[blocked]", "trace_leak_blocked") and e["blocked"].startswith(field + ":")      # 내용은 버리고 차단 사실만
    assert value.encode() not in raw_bytes(tmp_path)
    assert tracer.summary()["blocked_events"] == 1


def test_a_tool_call_records_name_ok_and_error_code_only(tracer, chunks, snapshot, tmp_path):   # noqa: F811
    masker = Masker(Inventory.from_file(FIXTURE))
    runner = FakeRunner(output=f"실행 결과 본문 {INSTANCE}")
    tb = ReadToolbox(masker=masker, guard=CommandGuard(CommandIndex(chunks), snapshot, workspace=tmp_path), retriever=HybridRetriever(chunks, vector_index=None),
                     runner=runner, budget=AgentBudget(Limits(max_tool_calls=4)), session_id="s9", search_kwargs={"method": "bm25"}, tracer=tracer)
    ph = masker.mask(INSTANCE)
    assert tb.call("search_docs", {"query": "인스턴스 재부팅"})["ok"]
    assert tb.call("run_read_command", {"command": f"aws ec2 describe-instances --instance-ids {ph}"})["ok"]
    assert tb.call("run_read_command", {"command": "aws ec2 reboot-instances --instance-ids " + ph})["ok"] is False                      # 변경 명령: 가드가 막는다
    assert tb.call("run_read_command", {"command": f"aws ec2 describe-instances --instance-ids {INSTANCE}"})["ok"] is False             # 실제 값: 마스킹 확인이 막는다
    with pytest.raises(BudgetExceeded):
        tb.call("search_docs", {"query": "하나 더"})                                                                                    # 5번째 호출은 한도 초과
    evs = list(reversed(tracer.events()))
    assert [(e["name"], e["ok"]) for e in evs] == [("search_docs", 1), ("run_read_command", 1), ("run_read_command", 0), ("run_read_command", 0), ("search_docs", 0)]
    assert [e["code"] for e in evs][2:] == ["not_executable_read_command", "command_not_masked", "tool_calls_exceeded"]
    data = raw_bytes(tmp_path)
    assert INSTANCE.encode() not in data and "실행 결과 본문".encode() not in data and "인스턴스 재부팅".encode() not in data and b"describe-instances" not in data
    s = tracer.summary()
    assert s["tool_calls"] == 5 and s["tool_errors"] == 3 and s["guard_blocks"] == 2 and s["guard_block_ratio"] == pytest.approx(0.4)
    tb2 = ReadToolbox(masker=masker, guard=tb._guard, retriever=tb._retriever, runner=runner, budget=AgentBudget(), tracer=tracer)
    from app.guards.agent_limits import ToolNotAllowed
    with pytest.raises(ToolNotAllowed):
        tb2.call("kubectl", {})
    assert tracer.events(limit=1)[0]["name"] == "unknown_tool"                                                                           # 모르는 도구 이름도 이름 그대로 남기지 않는다


def test_the_backend_is_chosen_by_trace_backend_and_unknown_ones_are_refused(tmp_path):
    assert isinstance(make_tracer(tmp_path / "a.sqlite", {}), LocalTracer)                          # 기본 local
    off = make_tracer(tmp_path / "off.sqlite", {"TRACE_BACKEND": "off"})
    assert isinstance(off, NullTracer) and not (tmp_path / "off.sqlite").exists()
    off.record("llm", "chat", input_tokens=1)
    assert off.events() == [] and off.summary() == {}
    with pytest.raises(TraceConfigError):
        make_tracer(tmp_path / "x.sqlite", {"TRACE_BACKEND": "langfuse"})                          # 이 단계에서는 구현하지 않았고 조용히 다른 백엔드로 바꾸지 않는다


def test_a_failing_trace_store_never_breaks_the_model_call(tmp_path):
    t = LocalTracer(tmp_path / "t.sqlite")
    t.close()                                                                                       # 닫힌 저장소에 기록해도 예외가 나가지 않는다
    r = llm({"m-main": ["ok"]}, t).complete("MAIN", "x")
    assert r.text == "ok"


def test_session_and_plan_hash_link_pipeline_events(tracer):
    h = "a" * 64
    tracer.record("pipeline", "plan", session_id="sess1", ok=True, n=2, plan_hash=h)
    tracer.record("pipeline", "stopped", session_id="sess2", ok=False, code="tool_calls_exceeded,x", n=0)
    (e,) = tracer.events(session_id="sess1")
    assert e["plan_hash"] == h and e["n"] == 2 and e["blocked"] == ""
    s = tracer.summary()
    assert s["pipelines"] == {"plan": 1, "stopped": 1} and s["repairs"] == 2


def test_the_database_has_no_free_text_columns(tracer, tmp_path):
    cols = {r[1]: r[2] for r in sqlite3.connect(tmp_path / "trace.sqlite").execute("PRAGMA table_info(trace_events)")}
    assert set(cols) >= {"session_id", "kind", "name", "code", "input_tokens", "plan_hash", "blocked"}
    assert not {c for c in cols if c in ("text", "prompt", "content", "output", "message", "request", "command", "args", "result")}
