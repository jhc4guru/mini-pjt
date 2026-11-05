"""실행기 단일 관문: 가드·승인 없이는 `_run`에 닿지 않는다. 요청 직렬화, 우회 경로가 없는지 정적 확인."""
import re
from pathlib import Path

import pytest

import app.executor.mock  # noqa: F401  (서브클래스 등록)
from app.executor.adapter import ReadRunnerAdapter
from app.executor.base import ExecutionRequest, ExecutionResult, Executor
from app.guards.cli_snapshot import CliSnapshot, CliUnavailable
from app.guards.command_guard import CommandGuard, CommandIndex
from app.guards.execution_policy import ExecutionDenied
from app.guards.masking import Inventory, Masker
from app.rag.chunks import load_chunks

FIXTURE = Path(__file__).parent / "fixtures" / "inventory.sample.yaml"
ROOT = Path(__file__).resolve().parent.parent
INSTANCE = "i-0abc123def4567890"


class RecordingExecutor(Executor):
    name = "recording"

    def __init__(self, guard):
        super().__init__(guard)
        self.ran: list[str] = []

    def _run(self, request):
        self.ran.append(request.command)
        return ExecutionResult(request_id=request.request_id, status="ok", exit_code=0, stdout="ok", executor=self.name)


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


@pytest.fixture()
def env(chunks, snapshot, tmp_path):
    masker = Masker(Inventory.from_file(FIXTURE))
    guard = CommandGuard(CommandIndex(chunks), snapshot, workspace=tmp_path)
    return guard, masker, RecordingExecutor(guard)


def prepare(guard, masker, real, kind=None):
    masked = masker.mask(real)
    v = guard.check(masked, masker)
    return v, ExecutionRequest.create(kind=kind or v.kind, masked_command=masked, command=real)


def test_read_command_runs_once_through_the_gate(env):
    guard, masker, ex = env
    v, req = prepare(guard, masker, f"aws ec2 describe-instances --instance-ids {INSTANCE}")
    assert ex.execute(req, verdict=v).status == "ok" and ex.ran == [req.command]


@pytest.mark.parametrize("real", ["aws ec2 describe-vpcs --profile prod", "aws ec2 describe-vpcs; rm -rf /", "aws sts get-caller-identity"])
def test_guard_blocked_commands_never_reach_run(env, real):
    guard, masker, ex = env
    masked = masker.mask(real)
    v = guard.check(masked, masker)
    req = ExecutionRequest.create(kind="read", masked_command=masked, command=real)
    with pytest.raises(ExecutionDenied) as e:
        ex.execute(req, verdict=v)
    assert e.value.code == "blocked_by_guard" and ex.ran == []


def test_manual_commands_are_never_executed_even_with_every_approval(env):
    guard, masker, ex = env
    real = "kubectl exec web-0 -- ls"
    v = guard.check(masker.mask(real), masker)
    req = ExecutionRequest.create(kind="mutate", masked_command=masker.mask(real), command=real)
    with pytest.raises(ExecutionDenied):
        ex.execute(req, verdict=v, approved_by_other=True, confirmations={"anything"})
    assert ex.ran == []


def test_mutating_command_needs_another_persons_approval(env):
    guard, masker, ex = env
    v, req = prepare(guard, masker, f"aws ec2 reboot-instances --instance-ids {INSTANCE}")
    with pytest.raises(ExecutionDenied) as e:
        ex.execute(req, verdict=v)
    assert e.value.code == "four_eyes_approval_required" and ex.ran == []
    assert ex.execute(req, verdict=v, approved_by_other=True).status == "ok" and len(ex.ran) == 1


def test_required_confirmations_must_be_present(env):
    guard, masker, ex = env
    sg = masker.mask("sg-0123456789abcdef0")
    v, req = prepare(guard, masker, f"aws ec2 authorize-security-group-ingress --group-id {'sg-0123456789abcdef0'} --protocol tcp --port 22 --cidr 0.0.0.0/0")
    assert v.requires
    with pytest.raises(ExecutionDenied) as e:
        ex.execute(req, verdict=v, approved_by_other=True)
    assert e.value.code.startswith("missing_confirmation") and ex.ran == []
    ex.execute(req, verdict=v, approved_by_other=True, confirmations=set(v.requires))
    assert len(ex.ran) == 1 and sg


def test_request_must_match_what_was_approved(env):
    guard, masker, ex = env
    v, req = prepare(guard, masker, f"aws ec2 describe-instances --instance-ids {INSTANCE}")
    other_text = req.model_copy(update={"masked_command": "aws ec2 describe-vpcs"})
    with pytest.raises(ExecutionDenied) as e:
        ex.execute(other_text, verdict=v)
    assert e.value.code == "request_differs_from_approved"
    other_kind = req.model_copy(update={"kind": "mutate"})
    with pytest.raises(ExecutionDenied):
        ex.execute(other_kind, verdict=v, approved_by_other=True)
    assert ex.ran == []


def test_tampered_command_is_detected_by_the_digest(env):
    guard, masker, ex = env
    v, req = prepare(guard, masker, f"aws ec2 describe-instances --instance-ids {INSTANCE}")
    tampered = req.model_copy(update={"command": "aws ec2 describe-vpcs"})          # 다이제스트는 그대로
    assert not tampered.digest_ok()
    with pytest.raises(ExecutionDenied) as e:
        ex.execute(tampered, verdict=v)
    assert e.value.code == "request_tampered" and ex.ran == []


def test_executing_side_rechecks_the_restored_command(env):
    """승인된 마스킹 문구는 읽기인데 실제 명령이 변경이면(문구와 다른 값으로 복원) 실행하는 쪽의 두 번째 가드가 막는다."""
    guard, masker, ex = env
    masked = masker.mask(f"aws ec2 describe-instances --instance-ids {INSTANCE}")
    v = guard.check(masked, masker)
    evil = f"aws ec2 terminate-instances --instance-ids {INSTANCE}"
    req = ExecutionRequest.create(kind="read", masked_command=masked, command=evil)         # 다이제스트는 evil로 맞게 만들어도
    with pytest.raises(ExecutionDenied) as e:
        ex.execute(req, verdict=v)
    assert e.value.code.startswith("recheck:") and ex.ran == []
    with pytest.raises(ExecutionDenied) as e2:
        ex.execute(req, verdict=v, approved_by_other=True)
    assert e2.value.code == "recheck:kind_changed" and ex.ran == []


def test_request_and_result_roundtrip_over_the_wire(env):
    guard, masker, _ = env
    _, req = prepare(guard, masker, f"aws ec2 describe-instances --instance-ids {INSTANCE}")
    assert ExecutionRequest.from_wire(req.to_wire()) == req and ExecutionRequest.from_wire(req.to_wire()).digest_ok()
    res = ExecutionResult(request_id=req.request_id, status="failed", exit_code=1, stderr="e", mock=True, executor="mock", notes=("a",))
    assert ExecutionResult.from_wire(res.to_wire()) == res
    with pytest.raises(Exception):
        ExecutionRequest.from_wire({**req.to_wire(), "extra": 1})


def test_adapter_runs_read_commands_only_through_the_gate(env):
    guard, masker, ex = env
    ad = ReadRunnerAdapter(ex, guard, masker)
    assert ad.run(f"aws ec2 describe-instances --instance-ids {INSTANCE}", timeout=5) == "ok"
    with pytest.raises(ExecutionDenied):
        ad.run(f"aws ec2 terminate-instances --instance-ids {INSTANCE}", timeout=5)       # 읽기 어댑터는 변경을 실행하지 못한다
    assert len(ex.ran) == 1


# ---------------- 우회 경로가 없는지 정적으로 확인 ----------------
def test_no_executor_subclass_overrides_the_gate():
    subs = Executor.__subclasses__()
    assert subs and all("execute" not in c.__dict__ for c in subs)


def test_run_is_called_only_from_the_gate():
    hits = []
    for p in (ROOT / "app").rglob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\._run\(request\)", line):
                hits.append(str(p.relative_to(ROOT)).replace("\\", "/"))
    assert hits == ["app/executor/base.py"], hits


def test_only_the_executor_and_snapshot_modules_spawn_processes():
    allowed = {"app/executor/process.py", "app/executor/moto_server.py", "app/guards/cli_snapshot.py"}
    found = {str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "app").rglob("*.py")
             if re.search(r"^\s*(?:import subprocess|from subprocess import)", p.read_text(encoding="utf-8"), re.M)}
    assert found <= allowed, found - allowed
