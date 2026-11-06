"""리뷰어: 체크리스트·위험 등급은 서버 규칙, 요약은 모델(참고용), 근거 조각 조회."""
import json
from pathlib import Path

import pytest

from app.guards.audit import AuditLog
from app.guards.cli_snapshot import CliSnapshot, CliUnavailable
from app.guards.command_guard import CommandGuard, CommandIndex
from app.guards.masking import Inventory, Masker
from app.guards.patterns import find_secrets, find_sensitive
from app.llm import LLMClient
from app.planner.review import REVIEWER_PROMPT, Reviewer, build_review, step_evidence
from app.planner.schema import PlanDraft, build_plan
from app.rag.chunks import load_chunks

FIXTURE = Path(__file__).parent / "fixtures" / "inventory.sample.yaml"
INSTANCE = "i-0abc123def4567890"


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeBedrock:
    def __init__(self, replies):
        self.replies, self.bodies = list(replies), []

    def converse(self, **kw):
        self.bodies.append(json.dumps(kw, ensure_ascii=False))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        text = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
        return {"output": {"message": {"content": [{"text": text}]}}, "usage": {"inputTokens": 5, "outputTokens": 5}}


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


@pytest.fixture(autouse=True)
def models(monkeypatch):
    for r in ("MAIN", "SUB", "READ"):
        monkeypatch.setenv(f"{r}_MODEL", f"m-{r.lower()}")


@pytest.fixture()
def env(chunks, snapshot, tmp_path):
    masker = Masker(Inventory.from_file(FIXTURE))
    return CommandGuard(CommandIndex(chunks), snapshot, workspace=tmp_path), masker, tmp_path


def S(id, command=None, kind="cli", **kw):
    return {"id": id, "kind": kind, "title": f"step {id}", "command": command, **kw}


def plan_of(env, steps, **phase):
    guard, masker, _ = env
    d = PlanDraft.model_validate({"summary": "테스트 계획", "phases": [{"id": "p1", "title": "phase", "steps": steps, **phase}]})
    return build_plan(d, guard, masker)


def reboot_plan(env, **phase):
    ph = env[1].mask(INSTANCE)
    return plan_of(env, [S("reboot", f"aws ec2 reboot-instances --instance-ids {ph}"),
                         S("verify", f"aws ec2 describe-instance-status --instance-ids {ph}", check_for="reboot")], **phase)


def codes(facts, status="warn"):
    return {i.code for i in facts.checklist if i.status == status}


GOOD = {"headline": "인스턴스 재부팅", "summary": "인스턴스 1대를 재부팅한다. 서비스 중단 가능성이 높다.", "attention": ["중단 가능성"],
        "phase_notes": [{"phase": "p1", "note": "재부팅과 상태 확인"}]}


def reviewer(env, replies):
    guard, masker, tmp = env
    fake = FakeBedrock(replies)
    audit = AuditLog(tmp / "a.sqlite")
    return Reviewer(LLMClient(masker, client=fake, sleep=lambda s: None), masker, audit=audit), fake, audit


# ---------------- 서버 규칙 ----------------
def test_reboot_plan_gets_disruption_gate_rollback_and_precondition_findings(env):
    facts = build_review(reboot_plan(env))
    assert {"disruption_likely", "no_gate", "no_rollback", "no_preconditions"} <= codes(facts)
    assert facts.risk_level == "medium" and "dry_run_available:reboot" in facts.warnings and "auto_step" in codes(facts, "info")


def test_a_well_formed_phase_has_no_gate_rollback_or_precondition_findings(env):
    plan = reboot_plan(env, gate=True, rollback="재시작으로 복구", preconditions=["점검 시간대"])
    assert codes(build_review(plan)) == {"disruption_likely"}


def test_irreversible_and_flagged_plans_are_high_risk(env):
    ph = env[1].mask(INSTANCE)
    plan = plan_of(env, [S("kill", f"aws ec2 terminate-instances --instance-ids {ph}"),
                         S("v", f"aws ec2 describe-instances --instance-ids {ph}", check_for="kill")])
    facts = build_review(plan)
    assert facts.risk_level == "high" and "irreversible" in codes(facts) and plan.phases[0].gate_locked
    sg = env[1].mask("sg-0123456789abcdef0")
    open_plan = plan_of(env, [S("open", f"aws ec2 authorize-security-group-ingress --group-id {sg} --protocol tcp --port 22 --cidr 0.0.0.0/0"),
                              S("v", f"aws ec2 describe-security-groups --group-ids {sg}", check_for="open")])
    f2 = build_review(open_plan)
    assert f2.risk_level == "high" and any(i.code == "risk_flag" and "open_cidr" in i.detail for i in f2.checklist)


def test_fallback_step_is_high_risk_and_listed(env):
    plan = plan_of(env, [S("odd", "aws ec2 describe-vpc-endpoints"), S("v", "aws ec2 describe-vpcs", check_for="odd")])
    facts = build_review(plan)
    assert facts.risk_level == "high" and "fallback_step" in codes(facts)


def test_read_only_plan_is_low_risk(env):
    facts = build_review(plan_of(env, [S("a", "aws ec2 describe-vpcs")]))
    assert facts.risk_level == "low" and not codes(facts)


# ---------------- 모델 요약 ----------------
def test_summary_is_returned_with_server_checklist_and_model_sees_only_facts(env):
    rv, fake, audit = reviewer(env, [GOOD])
    r = rv.review(reboot_plan(env), session_id="s1")
    assert r.summary == GOOD["summary"] and r.phase_notes == {"p1": "재부팅과 상태 확인"} and r.llm_roles == ("SUB->SUB",)
    body = fake.bodies[0]
    assert INSTANCE not in body and "<INSTANCE_1>" in body and '\\"risk_level\\": \\"medium\\"' in body and "disruption_likely" in body
    assert audit.query(session_id="s1")[0]["reasons"][0] == "review_summary"


def test_model_cannot_change_the_server_risk_level_or_checklist(env):
    rv, _, _ = reviewer(env, [{**GOOD, "summary": "위험 없음. 문제없다.", "headline": "안전"}])
    r = rv.review(reboot_plan(env))
    assert r.facts.risk_level == "medium" and "disruption_likely" in codes(r.facts)


def test_unknown_phase_notes_are_dropped(env):
    rv, _, _ = reviewer(env, [{**GOOD, "phase_notes": [{"phase": "p1", "note": "ok"}, {"phase": "ghost", "note": "x"}]}])
    assert rv.review(reboot_plan(env)).phase_notes == {"p1": "ok"}


def test_unsafe_or_invalid_model_output_leaves_the_checklist_without_a_summary(env):
    rv, _, _ = reviewer(env, [{**GOOD, "summary": f"{INSTANCE} 를 재부팅한다"}])
    r = rv.review(reboot_plan(env))
    assert r.summary is None and r.reasons == ("model_output_unsafe",) and r.facts.checklist
    rv, _, _ = reviewer(env, [{**GOOD, "summary": "<SG_9> 를 열었다"}])
    assert rv.review(reboot_plan(env)).reasons == ("model_output_unsafe",)                  # 지어낸 플레이스홀더
    rv, fake, _ = reviewer(env, ["not json", {"headline": 1}, {"x": 1}])
    r = rv.review(reboot_plan(env))
    assert r.summary is None and r.reasons == ("schema_violation",) and len(fake.bodies) == 3


def test_unavailable_model_does_not_block_the_review(env):
    t = ClientError("ThrottlingException")
    rv, _, _ = reviewer(env, [t] * 6)
    r = rv.review(reboot_plan(env))
    assert r.summary is None and r.reasons == ("llm_unavailable",) and r.facts.risk_level == "medium"


def test_reviewer_prompt_states_the_boundaries_and_has_no_sensitive_values():
    assert not find_sensitive(REVIEWER_PROMPT) and not find_secrets(REVIEWER_PROMPT)
    for phrase in ("참고용", "위험 등급", "체크리스트", "플레이스홀더", "입력에 있는 사실만", "되돌릴 수 없는", "AI 추정", "phase_notes"):
        assert phrase in REVIEWER_PROMPT, phrase


# ---------------- 근거 조각 ----------------
def test_step_evidence_returns_the_documentation_or_marks_ai_guess(env, chunks):
    by_id = {c.id: c for c in chunks}
    plan = plan_of(env, [S("odd", "aws ec2 describe-vpc-endpoints"), S("ok", "aws ec2 describe-vpcs", check_for="odd")])
    odd, ok = plan.all_steps()[-2:]
    ev = step_evidence(ok, by_id)
    assert ev["grounded"] and ev["chunk_id"] == "aws:ec2:describe-vpcs" and "describe-vpcs" in ev["text"]
    assert step_evidence(odd, by_id) == {"grounded": False, "label": "AI 추정", "chunk_id": None, "text": None}
    man = plan_of(env, [S("m", "kubectl exec web-0 -- ls", kind="manual")]).all_steps()[0]
    assert step_evidence(man, by_id)["grounded"] is True


def test_overlong_model_text_is_trimmed_instead_of_rejected(env):
    """실측: 큰 계획에서 모델이 길이 제한을 넘기면 스키마 위반으로 요약이 통째로 사라졌다. 길이는 자르고 내용은 살린다."""
    long = {"headline": "가" * 300, "summary": "나" * 2000, "attention": ["다" * 400] * 8,
            "phase_notes": [{"phase": "p1", "note": "라" * 500}]}
    rv, fake, _ = reviewer(env, [long])
    r = rv.review(reboot_plan(env))
    assert r.summary and len(r.headline) == 120 and len(r.summary) == 700 and len(r.attention) == 5 and all(len(a) == 160 for a in r.attention)
    assert len(r.phase_notes["p1"]) == 200 and len(fake.bodies) == 1


# ---------------- aws eks 비중 안내(D-45, 사용자 방침: kubectl 우선) ----------------
def test_many_aws_eks_steps_get_an_info_note_without_changing_the_risk_level(env):
    cl = env[1].mask("dev-main")
    reads = [S(f"e{i}", f"aws eks describe-cluster --name {cl}") for i in range(4)] + [S("k", "kubectl get nodes")]
    facts = build_review(plan_of(env, reads))
    note = [i for i in facts.checklist if i.code == "aws_eks_heavy"]
    assert len(note) == 1 and note[0].status == "info" and note[0].detail == "4/5" and note[0].steps == ("e0", "e1", "e2", "e3")
    assert facts.risk_level == "low" and not codes(facts)                                # 안내일 뿐 위험 등급과 경고에는 영향이 없다
    few = build_review(plan_of(env, [S("e0", f"aws eks describe-cluster --name {cl}"), S("k1", "kubectl get nodes"), S("k2", "kubectl version")]))
    assert not [i for i in few.checklist if i.code == "aws_eks_heavy"]                   # 적게 쓰면 안내하지 않는다
    kube = build_review(plan_of(env, [S("k1", "kubectl get nodes"), S("k2", "kubectl version")]))
    assert not [i for i in kube.checklist if i.code == "aws_eks_heavy"]


def test_a_released_gate_on_an_irreversible_phase_is_high_risk_and_shows_the_reason(env):
    """D-64: 사람이 사유를 적고 게이트를 해제한 되돌릴 수 없는 단계는 승인 화면에 경고(gate_waived)로 사유와 함께 나온다."""
    ph = env[1].mask(INSTANCE)
    base = plan_of(env, [S("kill", f"aws ec2 terminate-instances --instance-ids {ph}"), S("v", f"aws ec2 describe-instances --instance-ids {ph}", check_for="kill")])
    waived = base.model_copy(update={"phases": tuple(p.model_copy(update={"gate": False, "gate_locked": False, "gate_waiver": "점검 창에서 직접 진행"}) for p in base.phases)})
    facts = build_review(waived)
    waiver_items = [i for i in facts.checklist if i.code == "gate_waived"]
    assert facts.risk_level == "high" and waiver_items and waiver_items[0].detail == "점검 창에서 직접 진행"

