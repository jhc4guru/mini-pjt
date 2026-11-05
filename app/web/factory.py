"""실제 구성(Phase 1): 실제 Bedrock 모델 + mock 실행기(루프백 moto + 시뮬레이터) + SQLite 저장소.

- 실행기는 mock뿐이다(실제 AWS로 나가지 않는다). 세션마다 새 `Masker`·`LLMClient`·예산을 만들어 마스킹 매핑과 토큰 한도가 세션 사이에 섞이지 않게 한다.
- 모델 호출·읽기 도구는 모두 세션의 마스커를 거친다. 벡터 색인은 문서가 바뀐 조각만 다시 임베딩한다.
- `build_service()`를 호출하는 쪽이 `close()`로 실행기(moto 프로세스)와 DB를 닫는다.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from app import config
from app.agents.tools import ReadToolbox
from app.executor.adapter import ReadRunnerAdapter
from app.executor.mock import MockExecutor
from app.guards.agent_limits import AgentBudget
from app.guards.audit import AuditLog
from app.guards.cli_snapshot import CliSnapshot
from app.guards.command_guard import CommandGuard, CommandIndex
from app.guards.mapping_store import SqliteMappingStore
from app.guards.masking import Inventory, Masker
from app.llm import LLMClient
from app.planner.agent import PlanningAgent
from app.planner.failure import FailureAnalyzer
from app.planner.intake import Intake
from app.planner.pipeline import PlanPipeline
from app.planner.review import Reviewer
from app.planner.scratch_edit import ScratchEditor
from app.rag.chunks import load_chunks
from app.rag.factory import build_retriever
from app.session.service import SessionService
from app.session.store import SessionStore


@dataclass
class Running:
    service: SessionService
    store: SessionStore
    maps: SqliteMappingStore
    executor: MockExecutor
    audit: AuditLog

    def close(self) -> None:
        self.executor.close()
        self.store.close()
        self.maps.close()


def build_service(*, data_dir: Path | None = None, retriever=None) -> Running:
    config.load_env()
    data = Path(data_dir or config.DATA_DIR)
    data.mkdir(parents=True, exist_ok=True)
    chunks = load_chunks()
    by_id = {c.id: c for c in chunks}
    workspace = data / "workspace"
    workspace.mkdir(exist_ok=True)
    guard = CommandGuard(CommandIndex(chunks), CliSnapshot(), workspace=workspace)
    executor = MockExecutor(guard, Path(tempfile.mkdtemp(prefix="mock-exec-")))
    executor.seed()
    audit = AuditLog(data / "audit.sqlite")
    store = SessionStore(data / "sessions.sqlite")
    maps = SqliteMappingStore(data / "mapping.sqlite")
    retriever = retriever or build_retriever()

    def toolbox(masker: Masker, sid: str, budget: AgentBudget) -> ReadToolbox:
        return ReadToolbox(masker=masker, guard=guard, retriever=retriever, runner=ReadRunnerAdapter(executor, guard, masker, session_id=sid),
                           budget=budget, audit=audit, session_id=sid)

    def pipeline_factory(masker: Masker, sid: str) -> PlanPipeline:
        budget = AgentBudget()
        llm = LLMClient(masker, budget=budget)
        return PlanPipeline(Intake(llm, masker, audit=audit), PlanningAgent(llm, toolbox(masker, sid, budget), guard, masker, budget, audit=audit),
                            Reviewer(llm, masker, audit=audit), by_id)

    def analyzer_factory(masker: Masker) -> FailureAnalyzer:
        budget = AgentBudget()
        llm = LLMClient(masker, budget=budget)
        return FailureAnalyzer(llm, toolbox(masker, "", budget), guard, masker, budget, audit=audit)

    def editor_factory(masker: Masker) -> ScratchEditor:
        return ScratchEditor(LLMClient(masker, budget=AgentBudget()), masker, audit=audit)

    from app.rag.retriever import HybridRetriever
    service = SessionService(store, maps, executor=executor, guard=guard, pipeline_factory=pipeline_factory,
                             inventory_factory=Inventory.from_env, analyzer_factory=analyzer_factory, audit=audit,
                             catalog_retriever=HybridRetriever(chunks, vector_index=None),         # 명령 만들기 도우미: 모델을 쓰지 않는 BM25 검색
                             editor_factory=editor_factory)
    return Running(service, store, maps, executor, audit)
