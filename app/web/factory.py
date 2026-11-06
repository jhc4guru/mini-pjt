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
from app.planner.scenarios import load_scenarios
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
from app.observability import make_tracer
from app.usage import UsageLog
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
    usage: UsageLog | None = None
    tracer: object | None = None

    def close(self) -> None:
        self.executor.close()
        if self.tracer is not None:
            self.tracer.close()
        if self.usage is not None:
            self.usage.close()
        self.store.close()
        self.maps.close()


def build_service(*, data_dir: Path | None = None, retriever=None, usage_path: Path | None = None) -> Running:
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
    tracer = make_tracer(data / "trace.sqlite")                                    # 마스킹된 메타데이터만 남기는 로컬 트레이스(D-60, TRACE_BACKEND)
    usage = UsageLog(usage_path or data / "usage.sqlite")                       # 모델별 날짜별 토큰·쓰로틀링 기록(D-53)

    def toolbox(masker: Masker, sid: str, budget: AgentBudget) -> ReadToolbox:
        return ReadToolbox(masker=masker, guard=guard, retriever=retriever, runner=ReadRunnerAdapter(executor, guard, masker, session_id=sid),
                           budget=budget, audit=audit, session_id=sid, tracer=tracer)

    planner_mode = config.planner_mode()                                           # 계획 조사 방식(D-69). 잘못된 값이면 시작 때 오류
    scenarios = load_scenarios()                                                  # 시나리오 묶음(D-72, batch 방식에서만 쓴다). 파일이 규격에 맞지 않으면 시작 때 오류로 알린다

    def pipeline_factory(masker: Masker, sid: str) -> PlanPipeline:
        budget = AgentBudget()
        llm = LLMClient(masker, budget=budget, usage=usage, tracer=tracer, prompt_cache=True)      # 도구 루프(계획·실패 분석)만 프롬프트 캐싱을 쓴다(D-54)
        return PlanPipeline(Intake(llm, masker, audit=audit), PlanningAgent(llm, toolbox(masker, sid, budget), guard, masker, budget, audit=audit, mode=planner_mode,
                                                                                          scenarios=scenarios),
                            Reviewer(llm, masker, audit=audit), by_id)

    def analyzer_factory(masker: Masker) -> FailureAnalyzer:
        budget = AgentBudget()
        llm = LLMClient(masker, budget=budget, usage=usage, tracer=tracer, prompt_cache=True)      # 도구 루프(계획·실패 분석)만 프롬프트 캐싱을 쓴다(D-54)
        return FailureAnalyzer(llm, toolbox(masker, "", budget), guard, masker, budget, audit=audit)

    def editor_factory(masker: Masker) -> ScratchEditor:
        return ScratchEditor(LLMClient(masker, budget=AgentBudget(), usage=usage, tracer=tracer), masker, audit=audit)

    from app.rag.retriever import HybridRetriever
    service = SessionService(store, maps, executor=executor, guard=guard, pipeline_factory=pipeline_factory,
                             inventory_factory=Inventory.from_env, analyzer_factory=analyzer_factory, audit=audit,
                             catalog_retriever=HybridRetriever(chunks, vector_index=None),         # 명령 만들기 도우미: 모델을 쓰지 않는 BM25 검색
                             editor_factory=editor_factory, tracer=tracer)
    return Running(service, store, maps, executor, audit, usage, tracer)
