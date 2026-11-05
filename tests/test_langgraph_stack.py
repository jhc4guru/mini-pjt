"""에이전트 계층이 LangChain·LangGraph 위에 있다는 구조 검사(D-53). 직접 Bedrock Converse 호출로 되돌아가는 것을 막는다."""
import re
from pathlib import Path

from app.agents.tools import READ_TOOL_SPECS
from app.guards.agent_limits import READ_ONLY_TOOLS

APP = Path(__file__).resolve().parent.parent / "app"


def _src(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


def test_the_only_agent_model_call_is_langchain_chat_bedrock_converse():
    assert "ChatBedrockConverse" in _src("llm.py")
    # 검색의 리랭크(rag/reranker.py)는 D-53에서 이번 전환 범위 밖으로 둔 boto3 직접 호출이다. 에이전트 계층은 LLMClient 한 곳만 쓴다.
    allowed = {"rag/reranker.py"}
    offenders = [p.relative_to(APP).as_posix() for p in APP.rglob("*.py") if re.search(r"\.converse\(", p.read_text(encoding="utf-8"))]
    assert set(offenders) <= allowed, f"boto3 converse를 직접 부르는 파일: {offenders}(모델 호출은 LangChain ChatBedrockConverse, 에이전트는 app/llm.py의 LLMClient 한 곳)"


def test_tool_loop_and_pipeline_are_langgraph_state_graphs():
    from langgraph.graph.state import CompiledStateGraph
    from app.planner.pipeline import PlanPipeline
    from app.planner.toolloop import ToolLoop
    assert "StateGraph" in _src("planner/toolloop.py") and "StateGraph" in _src("planner/pipeline.py")
    loop = ToolLoop.__new__(ToolLoop)
    graph = ToolLoop._build_graph(loop)
    assert isinstance(graph, CompiledStateGraph) and {"model", "tools"} <= set(graph.get_graph().nodes)
    pipe = PlanPipeline.__new__(PlanPipeline)
    pg = PlanPipeline._build_graph(pipe)
    assert isinstance(pg, CompiledStateGraph) and {"intake", "plan", "review"} <= set(pg.get_graph().nodes)


def test_read_tools_are_langchain_tools_with_exactly_the_read_only_names():
    from langchain_core.tools import BaseTool
    assert set(READ_TOOL_SPECS) == READ_ONLY_TOOLS


def test_agents_do_not_import_unapproved_langgraph_parts():
    for p in APP.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "langgraph_supervisor" not in text and "langgraph.checkpoint" not in text, p
        assert "langchain_mcp_adapters" not in text and "fastmcp" not in text, p


def test_embedding_and_single_shot_agents_use_langchain_langgraph():
    assert "BedrockEmbeddings" in _src("rag/embedder.py") and "ChatBedrockConverse" in _src("rag/reranker.py")
    assert "invoke_model" not in _src("rag/embedder.py")
    for rel in ("planner/intake.py", "planner/review.py", "planner/scratch_edit.py"):
        assert "run_structured" in _src(rel) and "validate_structured" not in _src(rel), rel
    assert "StateGraph" in _src("planner/structured.py")
