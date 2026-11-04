"""에이전트의 읽기 전용 도구 세트: search_docs, lookup_inventory, run_read_command.

- 도구 이름은 `agent_limits.READ_ONLY_TOOLS`뿐이다. 그 밖의 이름은 `ToolNotAllowed`(변경 실행은 에이전트 도구가 아니다).
- 모든 호출은 `AgentBudget.before_tool`을 먼저 거친다(호출·시간 상한, 같은 호출 반복 감지). 상한 초과는 예외로 전파되어 중단된다.
- 모델이 보내는 인자는 마스킹된 것이어야 한다. 민감값이 보이면 도구를 실행하지 않고 오류를 돌려준다(재시도해도 같은 호출이라 반복 상한에 걸린다).
- 도구 결과는 신뢰하지 않는 데이터다. `sanitize_untrusted`(마스킹, 시크릿·지시문 제거, 데이터 틀)를 거쳐서만 모델에 돌아간다.
- `run_read_command`는 `CommandGuard.check` → `assert_read_only_tool_call` → `assert_server_executable`을 모두 통과한 문서 근거의 읽기 명령만 실행한다.
  실행은 주입된 `ReadCommandRunner`가 한다(T5의 실행기가 구현. 이 모듈은 어떤 프로세스도 직접 띄우지 않는다).
- 가드 차단·실행 실패는 모델이 대안을 찾을 수 있게 코드만 담은 오류로 돌려준다. 오류에 입력 원문이나 실제 값은 담지 않는다.
"""
from __future__ import annotations

from typing import Any, Protocol

from app.guards.agent_limits import READ_ONLY_TOOLS, AgentBudget, ToolNotAllowed
from app.guards.audit import AuditLog
from app.guards.boundary import LeakError, assert_outbound_clean
from app.guards.command_guard import CommandGuard
from app.guards.execution_policy import ExecutionDenied, assert_read_only_tool_call, assert_server_executable
from app.guards.masking import Masker
from app.guards.untrusted import sanitize_untrusted
from app.rag.retriever import HybridRetriever, SensitiveQueryError

MAX_QUERY_CHARS = 300
MAX_COMMAND_CHARS = 600
MAX_INVENTORY_ITEMS = 50
READ_TIMEOUT_SECONDS = 30.0


class ReadCommandRunner(Protocol):
    def run(self, command: str, *, timeout: float) -> str:
        """실제 값으로 복원된 읽기 명령을 실행하고 표준 출력을 돌려준다."""


def tool_specs() -> list[dict]:
    """Bedrock Converse `toolConfig`용 도구 정의. 이름은 READ_ONLY_TOOLS와 같아야 한다(테스트가 확인)."""
    def spec(name, desc, props, required):
        return {"toolSpec": {"name": name, "description": desc,
                             "inputSchema": {"json": {"type": "object", "properties": props, "required": required}}}}
    return [
        spec("search_docs", "CLI 문서(명령 단위)를 검색한다. 질의는 마스킹된 자연어다.",
             {"query": {"type": "string"}, "k": {"type": "integer", "minimum": 1, "maximum": 8}}, ["query"]),
        spec("lookup_inventory", "알려진 리소스 이름을 플레이스홀더 형태로 조회한다(카테고리: CLUSTER, BUCKET, ROLE 등).",
             {"category": {"type": "string"}}, ["category"]),
        spec("run_read_command", "문서에 근거한 읽기 명령을 실행해 결과를 본다. 변경 명령은 실행되지 않는다.",
             {"command": {"type": "string"}}, ["command"]),
    ]


class ReadToolbox:
    def __init__(
        self, *, masker: Masker, guard: CommandGuard, retriever: HybridRetriever, runner: ReadCommandRunner,
        budget: AgentBudget, audit: AuditLog | None = None, session_id: str | None = None,
        search_kwargs: dict | None = None,
    ):
        self._masker, self._guard, self._retriever, self._runner = masker, guard, retriever, runner
        self._budget, self._audit, self._session = budget, audit, session_id
        self._search_kwargs = search_kwargs or {}
        self._handlers = {"search_docs": self._search_docs, "lookup_inventory": self._lookup_inventory, "run_read_command": self._run_read_command}
        assert set(self._handlers) == READ_ONLY_TOOLS

    def call(self, name: str, args: dict[str, Any] | None = None) -> dict:
        args = args or {}
        if name not in READ_ONLY_TOOLS:
            raise ToolNotAllowed(f"허용되지 않은 도구: {name}")
        self._budget.before_tool(name, args)          # 상한 초과는 예외로 전파된다
        if not isinstance(args, dict):
            return self._err(name, "bad_arguments")
        return self._handlers[name](args)

    # ---- 도구 ----
    def _search_docs(self, args: dict) -> dict:
        query, k = args.get("query"), args.get("k", 5)
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS or not isinstance(k, int) or not 1 <= k <= 8 or set(args) - {"query", "k"}:
            return self._err("search_docs", "bad_arguments")
        try:
            assert_outbound_clean(query, self._masker, "tool:search_docs")
            result = self._retriever.search(query, k=k, **self._search_kwargs)
        except (LeakError, SensitiveQueryError):
            return self._err("search_docs", "query_not_masked")
        hits = [
            {
                "id": h.chunk.id, "command": h.chunk.command, "kind": h.chunk.kind, "risk": h.chunk.risk,
                "doc": sanitize_untrusted(h.chunk.text, self._masker, h.chunk.id).text,
            }
            for h in result.hits
        ]
        self._log("tool_result", True, ("search_docs",))
        return {"ok": True, "hits": hits, "ambiguous": [{"alias": a["alias"], "candidates": a["candidates"]} for a in result.ambiguous]}

    def _lookup_inventory(self, args: dict) -> dict:
        cat = args.get("category")
        names = self._masker.inventory.names
        if not isinstance(cat, str) or set(args) - {"category"} or cat.upper() not in names:
            return self._err("lookup_inventory", "unknown_category")
        # 실제 이름은 모델에 가지 않는다. 마스킹을 거친 플레이스홀더만 돌려준다(prod 여부도 서버 규칙이 판단하므로 싣지 않는다).
        items = [self._masker.mask(n) for n in names[cat.upper()][:MAX_INVENTORY_ITEMS]]
        self._log("tool_result", True, ("lookup_inventory",))
        return {"ok": True, "category": cat.upper(), "items": items}

    def _run_read_command(self, args: dict) -> dict:
        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > MAX_COMMAND_CHARS or set(args) - {"command"}:
            return self._err("run_read_command", "bad_arguments")
        try:
            assert_outbound_clean(cmd, self._masker, "tool:run_read_command")
        except LeakError:
            return self._err("run_read_command", "command_not_masked")
        if self._masker.unresolved(cmd):
            return self._err("run_read_command", "unknown_placeholder")
        verdict = self._guard.check(cmd, self._masker)
        self._log("command", verdict.allow, verdict.blocks, subject=cmd)
        try:
            assert_read_only_tool_call(verdict)
            assert_server_executable(verdict)
        except (ToolNotAllowed, ExecutionDenied):
            return {"ok": False, "error": "not_executable_read_command", "blocks": list(verdict.blocks), "kind": verdict.kind}
        try:
            raw = self._runner.run(self._masker.unmask(cmd), timeout=READ_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001  메시지는 올리지 않는다(실제 값·계정 정보가 섞일 수 있다)
            self._log("execution", False, (f"read_failed:{type(e).__name__}",))
            return self._err("run_read_command", "execution_failed")
        clean = sanitize_untrusted(raw, self._masker, "run_read_command")
        assert_outbound_clean(clean.text, self._masker, "tool_result:run_read_command")
        self._log("tool_result", True, ("run_read_command", *clean.flags))
        return {"ok": True, "command": cmd, "output": clean.text, "flags": list(clean.flags)}

    # ---- 보조 ----
    def _err(self, tool: str, code: str) -> dict:
        self._log("tool_result", False, (f"{tool}:{code}",))
        return {"ok": False, "error": code}

    def _log(self, stage: str, allowed: bool, reasons, subject: str | None = None) -> None:
        if self._audit is not None:
            self._audit.record(self._session, stage, allowed, tuple(reasons), subject=subject)
