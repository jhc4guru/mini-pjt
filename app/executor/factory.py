"""실행기 구성. Phase 1은 mock뿐이다. 실제 실행기는 Phase 3에서 같은 인터페이스로 추가한다(D-28)."""
from __future__ import annotations

import os
from pathlib import Path

from app.executor.base import Executor
from app.executor.mock import MockExecutor
from app.guards.command_guard import CommandGuard


def build_executor(guard: CommandGuard, workspace: Path) -> Executor:
    """`EXECUTOR_MODE`(기본 mock)에 맞는 실행기. mock 외의 값은 지원하지 않는다(실제 자격증명은 이 단계에서 쓰지 않는다)."""
    mode = os.environ.get("EXECUTOR_MODE", "mock").strip().lower()
    if mode != "mock":
        raise RuntimeError("Phase 1은 EXECUTOR_MODE=mock만 지원한다(실제 AWS 실행기는 Phase 3)")
    return MockExecutor(guard, workspace)
