"""계획 스키마: 모델은 초안만 만들고 분류·위험·게이트는 서버가 정한다. 구조 규칙과 해시."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.guards.cli_snapshot import CliSnapshot, CliUnavailable
from app.guards.command_guard import CommandGuard, CommandIndex
from app.guards.masking import Inventory, Masker
from app.planner.schema import PlanDraft, PlanInvalid, build_plan, plan_hash, step_hash
from app.rag.chunks import load_chunks

FIXTURE = Path(__file__).parent / "fixtures" / "inventory.sample.yaml"
INSTANCE = "i-0abc123def4567890"


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


def S(id, kind="cli", command=None, **kw):
    return {"id": id, "kind": kind, "title": f"step {id}", "command": command, **kw}


def draft(*phases):
    return PlanDraft.model_validate({"summary": "테스트 계획", "phases": [
        {"id": f"p{i}", "title": f"phase {i}", "steps": steps, **extra} for i, (steps, extra) in enumerate(phases, 1)]})


def masked(masker, cmd):
    return masker.mask(cmd)


def good(masker):
    return draft(([
        S("look", command=masked(masker, f"aws ec2 describe-instances --instance-ids {INSTANCE}")),
        S("reboot", command=masked(masker, f"aws ec2 reboot-instances --instance-ids {INSTANCE}"), depends_on=["look"]),
        S("verify", command=masked(masker, f"aws ec2 describe-instance-status --instance-ids {INSTANCE}"), check_for="reboot"),
    ], {}))


def codes_of(env, d, **kw):
    with pytest.raises(PlanInvalid) as e:
        build_plan(d, *env, **kw)
    return e.value.codes


def test_server_classifies_steps_and_model_cannot_set_op():
    with pytest.raises(ValidationError):
        PlanDraft.model_validate({"summary": "x", "phases": [{"id": "p1", "title": "t", "steps": [
            {"id": "a", "kind": "cli", "title": "t", "command": "aws ec2 describe-vpcs", "op": "read"}]}]})


def test_good_plan_is_enriched_from_guard_verdicts(env):
    guard, masker = env
    plan = build_plan(good(masker), guard, masker)
    look, reboot, verify = plan.all_steps()
    assert (look.op, reboot.op, verify.op) == ("read", "mutate", "read")
    assert reboot.grounded and reboot.chunk_id == "aws:ec2:reboot-instances" and reboot.impact["disruption"] == "likely"
    assert reboot.needs_other_approval and not look.needs_other_approval and not reboot.fallback


def test_kind_must_match_the_guard_verdict(env):
    _, masker = env
    d = draft(([S("x", kind="cli", command="kubectl exec web-0 -- ls")], {}))
    assert "kind_mismatch:x" in codes_of(env, d)
    d = draft(([S("x", kind="manual", command=masked(masker, "aws ec2 describe-vpcs"))], {}))
    assert "kind_mismatch:x" in codes_of(env, d)
    ok = build_plan(draft(([S("x", kind="manual", command="kubectl exec web-0 -- ls")], {})), *env)
    assert ok.all_steps()[0].op == "manual" and ok.all_steps()[0].needs_other_approval is False


def test_blocked_and_commandless_steps_are_invalid(env):
    c1 = codes_of(env, draft(([S("x", command="aws ec2 describe-vpcs --profile prod")], {})))
    c2 = codes_of(env, draft(([S("x", command="aws ec2 describe-vpcs; rm -rf /")], {})))
    assert any(c.startswith("blocked_command:x:") and "forbidden_option" in c for c in c1)       # 차단 사유가 코드에 실린다
    assert any(c.startswith("blocked_command:x:") and "shell_operator" in c for c in c2)
    assert "command_required:x" in codes_of(env, draft(([S("x")], {})))


def test_every_mutating_step_needs_a_following_check_in_the_same_phase(env):
    _, masker = env
    mut = S("reboot", command=masked(masker, f"aws ec2 reboot-instances --instance-ids {INSTANCE}"))
    chk = S("verify", command=masked(masker, f"aws ec2 describe-instance-status --instance-ids {INSTANCE}"), check_for="reboot")
    assert "missing_check:reboot" in codes_of(env, draft(([mut], {})))
    assert "missing_check:reboot" in codes_of(env, draft(([mut], {}), ([chk], {})))           # 다른 phase의 확인은 불가
    assert codes_of(env, draft(([chk, mut], {})))                                              # 확인이 변경보다 앞서면 불가
    build_plan(draft(([mut, chk], {})), *env)


def test_check_step_must_target_a_change_step(env):
    _, masker = env
    look = S("look", command=masked(masker, "aws ec2 describe-vpcs"))
    chk = S("c", command=masked(masker, "aws ec2 describe-vpcs"), check_for="look")
    assert "check_target_not_change:c" in codes_of(env, draft(([look, chk], {})))        # 읽기 step은 확인 대상이 아니다
    # 사람이 하는 변경(iac, manual)도 읽기 확인 step으로 검증할 수 있다
    iac = S("code", kind="iac", checklist=["PR 생성"])
    man = S("login", kind="manual", command="kubectl exec web-0 -- ls")
    ok = draft(([iac, S("c1", command=masked(masker, "aws ec2 describe-vpcs"), check_for="code"),
                 man, S("c2", command=masked(masker, "aws ec2 describe-vpcs"), check_for="login")], {}))
    build_plan(ok, *env)


def test_ids_and_references(env):
    _, masker = env
    r = masked(masker, "aws ec2 describe-vpcs")
    assert "duplicate_step_id:a" in codes_of(env, draft(([S("a", command=r), S("a", command=r)], {})))
    assert "unknown_or_forward_reference:a->zzz" in codes_of(env, draft(([S("a", command=r, depends_on=["zzz"])], {})))
    assert "unknown_or_forward_reference:a->b" in codes_of(env, draft(([S("a", command=r, depends_on=["b"]), S("b", command=r)], {})))
    with pytest.raises(ValidationError):
        draft(([S("Bad Id", command=r)], {}))


def test_irreversible_phase_gate_is_forced_on_and_locked(env):
    _, masker = env
    d = draft(([
        S("kill", command=masked(masker, f"aws ec2 terminate-instances --instance-ids {INSTANCE}")),
        S("verify", command=masked(masker, f"aws ec2 describe-instances --instance-ids {INSTANCE}"), check_for="kill"),
    ], {"gate": False}), ([S("look", command=masked(masker, "aws ec2 describe-vpcs"))], {"gate": False}))
    plan = build_plan(d, *env)
    assert plan.phases[0].gate and plan.phases[0].gate_locked and plan.phases[0].gate_reason
    assert not plan.phases[1].gate and not plan.phases[1].gate_locked


def test_undocumented_command_becomes_flagged_fallback_needing_other_approval(env):
    _, masker = env
    d2 = draft(([S("odd", command="aws ec2 describe-vpc-endpoints"),
                 S("verify", command=masked(masker, "aws ec2 describe-vpcs"), check_for="odd")], {}))
    plan = build_plan(d2, *env)
    odd = plan.all_steps()[0]
    assert odd.fallback and odd.needs_other_approval and "unknown_impact" in odd.flags and odd.op == "mutate"


def test_poll_requires_a_read_command_and_a_spec(env):
    _, masker = env
    cmd = masked(masker, "aws ec2 describe-vpcs")
    assert "poll_spec_required:p" in codes_of(env, draft(([S("p", kind="poll", command=cmd)], {})))
    spec = {"interval_seconds": 30, "max_seconds": 600, "until": "상태가 available"}
    assert "poll_spec_unexpected:c" in codes_of(env, draft(([S("c", command=cmd, poll=spec)], {})))
    ok = build_plan(draft(([S("p", kind="poll", command=cmd, poll=spec)], {})), *env)
    assert ok.all_steps()[0].poll.max_seconds == 600
    with pytest.raises(ValidationError):
        draft(([S("p", kind="poll", command=cmd, poll={**spec, "max_seconds": 99999})], {}))


def test_iac_and_manual_check_steps_need_no_command(env):
    d = draft(([S("code", kind="iac", checklist=["launch template 의 AMI 변경", "PR 생성"]),
                S("ok", kind="check", checklist=["terraform plan 결과 검토"])], {}))
    plan = build_plan(d, *env, iac_managed=True)
    assert [s.op for s in plan.all_steps()] == ["none", "none"] and plan.iac_managed


def test_size_limits_are_enforced(env):
    _, masker = env
    r = masked(masker, "aws ec2 describe-vpcs")
    with pytest.raises(ValidationError):                         # phase 상한은 스키마에서
        draft(([S(f"s{i}", command=r) for i in range(41)], {}))
    big = [([S(f"p{p}s{i}", command=r) for i in range(40)], {}) for p in range(4)]    # 160 > 120
    assert codes_of(env, draft(*big)) == ["plan_too_large"]


def test_model_output_with_real_values_is_rejected(env):
    d = draft(([S("a", command="aws ec2 describe-instances --instance-ids i-0abc123def4567890")], {}))
    assert codes_of(env, d) == ["model_output_unsafe"]


def test_hash_is_stable_and_covers_everything_the_approver_sees(env):
    guard, masker = env
    p1, p2 = build_plan(good(masker), guard, masker), build_plan(good(masker), guard, masker)
    assert plan_hash(p1) == plan_hash(p2)
    d = good(masker).model_dump()
    d["phases"][0]["steps"][1]["command"] = masked(masker, f"aws ec2 stop-instances --instance-ids {INSTANCE}")
    assert plan_hash(build_plan(PlanDraft.model_validate(d), guard, masker)) != plan_hash(p1)       # 명령이 바뀌면
    d = good(masker).model_dump()
    d["phases"][0]["steps"][0]["title"] = "다른 제목"
    assert plan_hash(build_plan(PlanDraft.model_validate(d), guard, masker)) != plan_hash(p1)       # 제목만 바뀌어도
    d = good(masker).model_dump()
    d["phases"][0]["rollback"] = "다른 롤백"
    assert plan_hash(build_plan(PlanDraft.model_validate(d), guard, masker)) != plan_hash(p1)       # 롤백이 바뀌어도
    assert step_hash(p1.all_steps()[0]) != step_hash(p1.all_steps()[1])


def test_declared_irreversible_iac_step_locks_the_gate_but_cannot_unlock_documented_ones(env):
    _, masker = env
    chk = S("c", command=masked(masker, "aws ec2 describe-vpcs"), check_for="up")
    plan = build_plan(draft(([S("up", kind="iac", checklist=["version 변경"], irreversible=True), chk], {"gate": False}),
                            ([S("look", command=masked(masker, "aws ec2 describe-vpcs"))], {})), *env)
    assert plan.phases[0].gate and plan.phases[0].gate_locked and plan.phases[0].steps[0].irreversible
    assert not plan.phases[1].gate_locked
    plain = build_plan(draft(([S("up", kind="iac", checklist=["version 변경"]), chk], {"gate": False})), *env)
    assert not plain.phases[0].gate and not plain.phases[0].gate_locked
    # 문서가 irreversible인 명령은 모델이 irreversible=false로 써도 잠긴다
    inst = masker.mask("i-0abc123def4567890")
    kill = draft(([S("kill", command=f"aws ec2 terminate-instances --instance-ids {inst}", irreversible=False),
                   S("v", command=f"aws ec2 describe-instances --instance-ids {inst}", check_for="kill")], {"gate": False}))
    assert build_plan(kill, *env).phases[0].gate_locked


def test_gate_of_an_irreversible_phase_is_released_only_by_a_human_reason_never_by_the_model(env):
    """D-64: 되돌릴 수 없는 step이 있는 단계의 게이트는 사람이 적은 사유(gate_waivers)와 gate=false가 함께 있을 때만 해제된다. 모델 출력만으로는 해제되지 않는다."""
    _, masker = env
    inst = masker.mask("i-0abc123def4567890")
    d = draft(([S("kill", command=f"aws ec2 terminate-instances --instance-ids {inst}"),
                S("v", command=f"aws ec2 describe-instances --instance-ids {inst}", check_for="kill")], {"gate": False}))
    assert build_plan(d, *env).phases[0].gate                                                  # 모델이 gate=false로 써도 켜진다
    assert build_plan(d, *env, gate_waivers={"p1": "  "}).phases[0].gate                          # 빈 사유는 해제가 아니다
    waived = build_plan(d, *env, gate_waivers={"p1": "사람이 직접 진행"})
    ph = waived.phases[0]
    assert not ph.gate and not ph.gate_locked and ph.gate_waiver == "사람이 직접 진행" and any(s.irreversible for s in ph.steps)
    kept = draft(([S("kill", command=f"aws ec2 terminate-instances --instance-ids {inst}"),
                   S("v", command=f"aws ec2 describe-instances --instance-ids {inst}", check_for="kill")], {"gate": True}))
    assert build_plan(kept, *env, gate_waivers={"p1": "사유"}).phases[0].gate                       # 게이트를 켜 둔 단계에는 사유가 남지 않는다
    assert plan_hash(waived) != plan_hash(build_plan(d, *env))                                    # 해제는 해시(승인 잠금)에 반영된다


@pytest.mark.parametrize("title", ["Terraform 코드에서 kubernetes_version을 1.36으로 변경", "컨트롤 플레인 업그레이드 적용", "EKS control plane upgrade apply",
                                   "terraform destroy 로 클러스터 정리", "클러스터 삭제"])
def test_irreversible_looking_iac_step_without_the_mark_is_rejected(env, title):
    """D-64: 표시가 빠지기 쉬운 되돌릴 수 없는 iac 작업은 `irreversible` 표시가 없으면 검증 실패로 돌려 모델이 고치게 한다."""
    _, masker = env
    chk = S("c", command=masked(masker, "aws ec2 describe-vpcs"), check_for="up")
    with pytest.raises(PlanInvalid) as e:
        build_plan(draft(([S("up", kind="iac", title=title, checklist=["코드 변경"]), chk], {})), *env)
    assert "irreversible_not_marked:up" in e.value.codes
    ok = build_plan(draft(([S("up", kind="iac", title=title, checklist=["코드 변경"], irreversible=True), chk], {})), *env)
    assert ok.phases[0].gate and ok.phases[0].gate_locked


@pytest.mark.parametrize("title", ["Terraform 코드 확인", "add-on 버전 변경", "노드 AMI 변경", "컨트롤 플레인 상태 확인"])
def test_ordinary_iac_steps_are_not_flagged_as_irreversible(env, title):
    _, masker = env
    chk = S("c", command=masked(masker, "aws ec2 describe-vpcs"), check_for="up")
    plan = build_plan(draft(([S("up", kind="iac", title=title, checklist=["코드 변경"]), chk], {})), *env)
    assert not plan.phases[0].gate_locked

