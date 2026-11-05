"""앞 절차 결과 참조(D-48): `@{절차.필드}`는 템플릿으로 승인되고, 실행 직전에 서버가 리소스 ID만 꺼내 치환하며, 치환한 명령은 가드·관문을 다시 통과한다."""
import json

import pytest
from test_apply import INSTANCE, chunks, denied, e, snapshot, start  # noqa: F401
from test_plan_postprocess import S as step, draft

from app.executor.base import ExecutionRequest
from app.guards.execution_policy import ExecutionDenied
from app.guards.masking import Masker
from app.guards.ref_resolve import RefError, resolve
from app.guards.refs import find_refs, has_unparsed_ref, with_dummy
from app.planner.schema import PlanInvalid, build_plan

SG = "sg-0123456789abcdef0"
MK = {"id": "mk-sg", "kind": "cli", "title": "SG 생성", "command": "aws ec2 create-security-group --group-name web --description web"}
MK_CHK = {"id": "mk-chk", "kind": "cli", "title": "SG 확인", "command": "aws ec2 describe-security-groups --group-ids @{mk-sg.GroupId}", "check_for": "mk-sg"}


def rule_for(e):
    cidr = e.masker.mask("10.20.0.0/16")                                                               # 실제 CIDR은 마스킹된 플레이스홀더로만 쓴다
    return {"id": "rule", "kind": "cli", "title": "규칙 추가", "command": f"aws ec2 authorize-security-group-ingress --group-id @{{mk-sg.GroupId}} --protocol tcp --port 443 --cidr {cidr}"}


RULE_CHK = {"id": "rule-chk", "kind": "cli", "title": "규칙 확인", "command": "aws ec2 describe-security-groups --group-ids @{mk-sg.GroupId}", "check_for": "rule"}


# ---------------- 문법·치환 ----------------
def test_references_are_found_with_their_step_and_json_path_and_odd_ones_are_flagged():
    assert find_refs("aws ec2 x --group-id @{mk.GroupId} --id @{run.Instances[0].InstanceId}") == [("mk", "GroupId"), ("run", "Instances[0].InstanceId")]
    assert find_refs("aws ec2 x") == [] and not has_unparsed_ref("aws ec2 x")
    for odd in ("@{mk}", "@{mk.}", "@{mk.a b}", "@{mk.a;b}", "@{.a}", "@{mk.$(id)}"):
        assert has_unparsed_ref("aws ec2 x " + odd), odd
    assert "@{" not in with_dummy("aws ec2 x --group-id @{mk.GroupId}")


def test_only_resource_ids_are_taken_from_the_output_and_they_go_in_as_placeholders():
    m = Masker()
    out = lambda _i: json.dumps({"GroupId": SG, "Name": "web", "Rules": [{"id": "sgr-0123456789abcdef0"}], "Count": 3})   # noqa: E731
    got = resolve("aws ec2 authorize-security-group-ingress --group-id @{mk.GroupId}", out, m)
    assert SG not in got and "<SG_1>" in got and m.unmask(got).endswith(SG)                        # 실제 값은 플레이스홀더로 들어간다
    assert "<" in resolve("c @{mk.Rules[0].id}", out, m)
    for path in ("Name", "Count", "Rules", "Missing", "Rules[5].id"):                                # 이름·숫자·객체·없는 경로·범위 밖 인덱스는 거부
        with pytest.raises(RefError):
            resolve(f"c @{{mk.{path}}}", out, m)
    for hostile in ("web; rm -rf /", "sg-123", "$(whoami)", "--profile admin", "10.0.0.1", "arn:aws:iam::123456789012:role/x"):
        with pytest.raises(RefError) as ex:
            resolve("c @{mk.GroupId}", lambda _i, h=hostile: json.dumps({"GroupId": h}), m)
        assert ex.value.code == "ref:value_not_resource_id"


def test_an_output_that_is_not_json_or_has_a_lookalike_placeholder_is_refused():
    m = Masker()
    with pytest.raises(RefError):
        resolve("c @{mk.GroupId}", lambda _i: "not json", m)
    with pytest.raises(RefError):
        resolve("c @{mk.GroupId}", lambda _i: json.dumps({"GroupId": "<SG_9>"}), m)                    # 아는 값이 아닌 플레이스홀더


# ---------------- 가드·관문 ----------------
def test_the_guard_checks_the_template_with_a_fake_id_and_keeps_the_template_text(e):
    RULE = rule_for(e)
    v = e.guard.check(RULE["command"], e.masker)
    assert v.allow and v.command == RULE["command"] and "uses_output" in v.flags and v.kind == "mutate"
    assert not e.guard.check("aws ec2 describe-vpcs --vpc-ids @{mk.bad value}", e.masker).allow
    assert e.guard.check("aws ec2 describe-vpcs --profile x --vpc-ids @{mk.VpcId}", e.masker).allow is False      # 금지 옵션은 템플릿에서도 막힌다


def test_the_executor_refuses_anything_that_still_has_a_reference(e):
    RULE = rule_for(e)
    v = e.guard.check(RULE["command"], e.masker)
    req = ExecutionRequest.create(kind="mutate", masked_command=RULE["command"], command=RULE["command"], session_id=e.sid, plan_id="p", step_id="rule", timeout_seconds=5)
    with pytest.raises(ExecutionDenied) as ex:
        e.ex.execute(req, verdict=v, approved_by_other=True)
    assert ex.value.code == "unresolved_reference" and e.ex.ran == []


# ---------------- plan 검증 ----------------
def test_a_reference_adds_the_dependency_and_must_point_at_an_earlier_server_step(e):
    RULE = rule_for(e)
    plan = e.make_plan((MK, MK_CHK, RULE, RULE_CHK))
    rule = next(s for s in plan.all_steps() if s.id == "rule")
    assert "mk-sg" in rule.depends_on and rule.command == RULE["command"]                             # 의존 자동 추가, 템플릿은 그대로 저장
    bad = lambda *steps: e.make_plan(steps)                                                           # noqa: E731
    with pytest.raises(PlanInvalid) as ex:
        bad({**RULE, "id": "early"}, MK)                                                              # 아직 없는(뒤에 나오는) 절차
    assert any(c.startswith("ref_unknown_or_forward:early->mk-sg") for c in ex.value.codes)
    with pytest.raises(PlanInvalid) as ex:
        bad(MK, {"id": "hum", "kind": "iac", "title": "코드", "checklist": ["x"]}, {**RULE, "id": "r2", "command": RULE["command"].replace("mk-sg", "hum")})
    assert any(c.startswith("ref_target_not_runnable:r2->hum") for c in ex.value.codes)
    with pytest.raises(PlanInvalid) as ex:
        bad(MK, {"id": "man", "kind": "manual", "title": "수동", "command": "kubectl exec web-0 -- ls @{mk-sg.GroupId}"})
    assert any(c.startswith("ref_not_allowed_here:man") for c in ex.value.codes) or any(c.startswith("blocked_command:man") for c in ex.value.codes)


def test_a_referenced_step_must_keep_json_output(e):
    for cmd in ("aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value", "aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --output text"):
        with pytest.raises(PlanInvalid) as ex:
            e.make_plan(({"id": "get", "kind": "cli", "title": "조회", "command": cmd},
                         {"id": "use", "kind": "cli", "title": "사용", "command": "aws ec2 describe-images --image-ids @{get.Parameter.Value}"}))
        assert any(c.startswith("ref_target_not_json:use->get") for c in ex.value.codes), cmd


# ---------------- apply: 실행 직전 치환 ----------------
def run_chain(e, outputs):
    plan = e.make_plan((MK, MK_CHK, rule_for(e), RULE_CHK))
    v, h = start(e, plan)
    e.ex.script = outputs
    e.ap.approve_step(e.sid, "bob", v, "mk-sg", h)
    return v, h


def test_the_created_resource_id_is_substituted_into_later_steps_and_everything_is_recorded_masked(e):
    v, h = run_chain(e, [("ok", json.dumps({"GroupId": SG}), "")])
    out = e.apply.run_step(e.sid, v, "mk-sg", nick="bob")
    assert out.state == "done" and [c.step_id for c in out.checks] == ["mk-chk"]                       # 확인 절차의 참조도 치환된다
    assert [r.step_id for r in e.ex.ran] == ["mk-sg", "mk-chk"] and SG in e.ex.ran[1].command and "@{" not in e.ex.ran[1].command
    e.ap.approve_step(e.sid, "bob", v, "rule", h)
    out2 = e.apply.run_step(e.sid, v, "rule", nick="bob")
    assert out2.state == "done"
    ran = e.ex.ran[2]
    assert ran.step_id == "rule" and SG in ran.command and "@{" not in ran.command and SG not in ran.masked_command and "<SG_" in ran.masked_command
    stored = {x.id: x.command for x in e.store.load_plan(e.sid, v).all_steps()}
    assert stored["rule"].count("@{mk-sg.GroupId}") == 1                                               # 저장·승인된 것은 템플릿이다
    log = [x for x in e.store.events(e.sid) if x["kind"] == "step_ref_resolved"]
    assert log and all(SG not in x["detail"] for x in log) and any(x["detail"].startswith("rule:") for x in log)        # 이력에는 마스킹된 명령만
    text = e.store.outputs(e.sid, v, "mk-sg")[0]["text"]
    assert SG not in text and "<SG_" in text


@pytest.mark.parametrize("output,reason", [
    (json.dumps({"Other": "x"}), "path_not_found"),
    (json.dumps({"GroupId": "web; rm -rf /"}), "value_not_resource_id"),
    ("not json at all", "path_not_found"),
])
def test_a_missing_or_non_id_value_stops_the_step_without_running_it(e, output, reason):
    v, h = run_chain(e, [("ok", output, "")])
    e.apply.run_step(e.sid, v, "mk-sg", nick="bob")
    # mk-chk(확인)가 자동 실행되다 실패해 세션이 멈춘다: 참조를 못 푼 명령은 실행기에 닿지 않는다
    assert [r.step_id for r in e.ex.ran] == ["mk-sg"]
    st = e.store.step_states(e.sid, v)["mk-chk"]
    assert st["state"] == "failed" and st["detail"] == f"denied:ref:{reason}"
    assert e.store.session(e.sid)["state"] == "paused"


def test_a_step_cannot_resolve_a_reference_before_the_referenced_step_is_done(e):
    plan = e.make_plan((MK, MK_CHK, rule_for(e), RULE_CHK))
    v, h = start(e, plan)
    e.ap.approve_step(e.sid, "bob", v, "mk-sg", h)
    with pytest.raises(Exception):
        e.ap.approve_step(e.sid, "bob", v, "rule", h)                                                # 의존한 절차가 끝나기 전에는 승인·실행되지 않는다
    assert e.ex.ran == []


def test_a_read_that_uses_a_reference_cannot_be_previewed_during_review(e):
    plan = e.make_plan((MK, MK_CHK, rule_for(e), RULE_CHK))
    v = e.ap.submit_plan(e.sid, "alice", plan)
    denied("preview_has_references", e.apply.preview_read, e.sid, v, "mk-chk", nick="bob")
