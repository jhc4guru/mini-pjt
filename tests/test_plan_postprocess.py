"""계획 후처리: 사전 점검 읽기 step 자동 삽입, IaC 동기화 step, 경고."""
from pathlib import Path

import pytest

from app.guards.cli_snapshot import CliSnapshot, CliUnavailable
from app.guards.command_guard import CommandGuard, CommandIndex
from app.guards.masking import Inventory, Masker
from app.planner.schema import PlanDraft, PlanInvalid, build_plan, plan_hash
from app.rag.chunks import load_chunks

FIXTURE = Path(__file__).parent / "fixtures" / "inventory.sample.yaml"
INSTANCE, SG = "i-0abc123def4567890", "sg-0123456789abcdef0"


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
    return CommandGuard(CommandIndex(chunks), snapshot, workspace=tmp_path), masker


def S(id, command=None, kind="cli", **kw):
    return {"id": id, "kind": kind, "title": f"step {id}", "command": command, **kw}


def draft(*steps_per_phase):
    return PlanDraft.model_validate({"summary": "테스트", "phases": [
        {"id": f"p{i}", "title": f"phase {i}", "steps": steps} for i, steps in enumerate(steps_per_phase, 1)]})


def assert_well_formed(plan):
    """더한 step이 있어도 id는 유일하고 참조는 앞선 step만 가리킨다."""
    seen = set()
    for s in plan.all_steps():
        assert s.id not in seen
        for ref in (*s.depends_on, *([s.check_for] if s.check_for else [])):
            assert ref in seen, (s.id, ref)
        seen.add(s.id)


def reboot_draft(m, *, look=False):
    ph = m.mask(INSTANCE)
    steps = []
    if look:
        steps.append(S("look", f"aws ec2 describe-instances --instance-ids {ph}"))
    steps += [S("reboot", f"aws ec2 reboot-instances --instance-ids {ph}"),
              S("verify", f"aws ec2 describe-instance-status --instance-ids {ph}", check_for="reboot")]
    return draft(steps)


def test_missing_preflight_is_inserted_at_the_start_of_the_phase(env):
    guard, m = env
    plan = build_plan(reboot_draft(m), guard, m)
    pre, reboot, verify = plan.all_steps()
    assert pre.auto and pre.op == "read" and pre.grounded and pre.chunk_id == "aws:ec2:describe-instances"
    assert pre.command == "aws ec2 describe-instances --instance-ids <INSTANCE_1>" and "자동 삽입" in pre.rationale
    assert pre.id in reboot.depends_on and not reboot.auto and not verify.auto
    assert "dry_run_available:reboot" in plan.warnings
    assert_well_formed(plan)


def test_existing_preflight_before_the_change_is_not_duplicated(env):
    guard, m = env
    plan = build_plan(reboot_draft(m, look=True), guard, m)
    assert [s.id for s in plan.all_steps()] == ["look", "reboot", "verify"] and not any(s.auto for s in plan.all_steps())


def test_a_read_after_the_change_does_not_count_as_preflight(env):
    guard, m = env
    ph = m.mask(INSTANCE)
    d = draft([S("reboot", f"aws ec2 reboot-instances --instance-ids {ph}"),
               S("verify", f"aws ec2 describe-instances --instance-ids {ph}", check_for="reboot")])
    plan = build_plan(d, guard, m)
    assert plan.all_steps()[0].auto and len(plan.all_steps()) == 3


def test_unbuildable_preflight_is_a_warning_not_an_invented_command(env):
    guard, m = env
    asg = m.mask("eks-workers-a")
    d = draft([S("scale", f"aws autoscaling set-desired-capacity --auto-scaling-group-name {asg} --desired-capacity 3"),
               S("verify", f"aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names {asg}", check_for="scale")])
    plan = build_plan(d, guard, m)
    assert not any(s.auto for s in plan.all_steps())
    assert "preflight_not_inserted:scale:describe-auto-scaling-groups" in plan.warnings


def test_kubectl_and_s3_preflights_are_built_from_the_target_and_namespace(env):
    guard, m = env
    ns, bucket = m.mask("payments"), m.mask("acme-logs")
    d = draft([S("scale", f"kubectl scale deployment/web --replicas=2 --namespace {ns}"),
               S("verify", f"kubectl get deployment/web --namespace {ns}", check_for="scale")],
              [S("rb", f"aws s3 rb s3://{bucket}"), S("chk", f"aws s3 ls s3://{bucket}", check_for="rb")])
    plan = build_plan(d, guard, m)
    autos = [s.command for s in plan.all_steps() if s.auto]
    assert f"kubectl get deployment/web --namespace {ns}" in autos and f"aws s3 ls s3://{bucket}" in autos
    assert_well_formed(plan)


def test_iac_sync_step_follows_the_check_when_iac_is_managed(env):
    guard, m = env
    sg, cidr = m.mask(SG), m.mask("10.20.0.0/16")
    d = draft([S("open", f"aws ec2 authorize-security-group-ingress --group-id {sg} --protocol tcp --port 443 --cidr {cidr}"),
               S("verify", f"aws ec2 describe-security-groups --group-ids {sg}", check_for="open")])
    plan = build_plan(d, guard, m, iac_managed=True)
    ids = [s.id for s in plan.all_steps()]
    iac = next(s for s in plan.all_steps() if s.kind == "iac")
    assert iac.auto and iac.op == "none" and iac.depends_on == ("open",) and iac.checklist
    assert ids.index(iac.id) == ids.index("verify") + 1                    # 확인 step 바로 뒤
    assert {"iac_drift:open", "consider_iac_step:open"} <= set(plan.warnings)
    assert_well_formed(plan)
    off = build_plan(d, guard, m, iac_managed=False)
    assert not any(s.kind == "iac" for s in off.all_steps()) and "iac_drift:open" not in off.warnings


def test_existing_iac_step_for_the_change_is_not_duplicated(env):
    guard, m = env
    sg, cidr = m.mask(SG), m.mask("10.20.0.0/16")
    d = draft([S("open", f"aws ec2 authorize-security-group-ingress --group-id {sg} --protocol tcp --port 443 --cidr {cidr}"),
               S("verify", f"aws ec2 describe-security-groups --group-ids {sg}", check_for="open"),
               S("code", kind="iac", depends_on=["open"], checklist=["PR 생성"])])
    plan = build_plan(d, guard, m, iac_managed=True)
    assert [s.kind for s in plan.all_steps()].count("iac") == 1 and not any(s.auto and s.kind == "iac" for s in plan.all_steps())


def test_auto_steps_and_warnings_change_the_plan_hash(env):
    guard, m = env
    d = reboot_draft(m)
    assert plan_hash(build_plan(d, guard, m)) != plan_hash(build_plan(d, guard, m, postprocess=False))
    assert plan_hash(build_plan(d, guard, m)) == plan_hash(build_plan(d, guard, m))


def test_size_limit_is_checked_again_after_auto_steps(env):
    guard, m = env
    ph = m.mask(INSTANCE)
    steps = [S(f"r{i}", "aws ec2 describe-vpcs") for i in range(38)]
    steps += [S("reboot", f"aws ec2 reboot-instances --instance-ids {ph}"),
              S("verify", f"aws ec2 describe-instance-status --instance-ids {ph}", check_for="reboot")]
    assert len(steps) == 40
    with pytest.raises(PlanInvalid) as e:
        build_plan(draft(steps), guard, m)
    assert e.value.codes == ["plan_too_large_after_auto_steps"]
    assert build_plan(draft(steps), guard, m, postprocess=False)                # 모델 초안 자체는 상한 안이다
