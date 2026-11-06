"""시나리오 묶음(D-72): 파일 규격, 요청 매칭, 조사 명령이 실제 가드·문서와 맞는지, 앱 팩토리가 방식·시나리오를 에이전트에 넘기는지.
에이전트 흐름(조사·윤곽 대체, 필수 항목 검사, 이어하기)은 `test_planning_agent.py`에 있다."""
import copy
from pathlib import Path

import pytest
import yaml

from app.guards.cli_snapshot import CliSnapshot, CliUnavailable
from app.guards.command_guard import CommandGuard, CommandIndex
from app.guards.execution_policy import assert_read_only_tool_call, assert_server_executable
from app.guards.masking import Inventory, Masker
from app.planner.scenarios import (MAX_DOCS_PER_PHASE, SCENARIO_DIR, ScenarioError, load_scenarios, match_scenario, missing_expectations,
                                   parse_scenario, validate_structure)
from app.rag.chunks import load_chunks
from app.rag.retriever import HybridRetriever

FIXTURE = Path(__file__).parent / "fixtures" / "inventory.sample.yaml"
REQ = "<CLUSTER_1> 클러스터를 1.35에서 1.36으로 업그레이드하는 계획을 세워줘. 노드는 ASG <ASG_1> 로 관리해"


@pytest.fixture(scope="module")
def chunks():
    return load_chunks()


@pytest.fixture(scope="module")
def eks():
    (sc,) = [s for s in load_scenarios() if s.id == "eks-upgrade"]
    return sc


@pytest.fixture(scope="module")
def raw():
    return yaml.safe_load((SCENARIO_DIR / "eks-upgrade.yaml").read_text(encoding="utf-8"))


# ---------------- 규격 ----------------
def test_shipped_scenarios_load_and_have_matching_file_names():
    found = load_scenarios()
    assert found and all((SCENARIO_DIR / f"{s.id}.yaml").exists() for s in found)


def test_gates_need_a_reason_and_other_structure_errors_are_rejected(raw):
    def broken(mutate):
        d = copy.deepcopy(raw)
        mutate(d)
        return d
    with pytest.raises(ScenarioError, match="사유"):
        parse_scenario(broken(lambda d: d["phases"][1].pop("gate_reason")))
    with pytest.raises(ScenarioError, match="id가 겹친다"):
        parse_scenario(broken(lambda d: d["phases"][1].__setitem__("id", "preflight")))
    with pytest.raises(ScenarioError, match="정의하지 않은 param"):
        parse_scenario(broken(lambda d: d["investigation"].append("aws eks describe-cluster --name {nope}")))
    with pytest.raises(ScenarioError, match="aws·kubectl만"):
        parse_scenario(broken(lambda d: d["investigation"].append("terraform plan")))
    with pytest.raises(ScenarioError, match="정규식"):
        parse_scenario(broken(lambda d: d["phases"][0]["expect"].append(["("])))
    with pytest.raises(ScenarioError, match="docs는"):
        parse_scenario(broken(lambda d: d["phases"][0].__setitem__("docs", [f"q{i}" for i in range(MAX_DOCS_PER_PHASE + 1)])))
    with pytest.raises(ScenarioError):
        parse_scenario({"id": "x"})                                                       # 필수 항목 없음


def test_eks_upgrade_skeleton_is_the_reviewed_one(eks):
    """게이트·필수 항목은 사람이 검토한 값이다. 바꾸려면 사용자 검토와 DECISIONS 갱신이 필요하므로 테스트도 함께 고친다."""
    assert [(p.id, p.gate) for p in eks.phases] == [("preflight", False), ("control-plane-upgrade", True), ("addon-upgrade", True), ("node-replace", True), ("final-verify", False)]
    assert not validate_structure(eks)
    cp = eks.phases[1]
    assert "되돌릴 수 없" in cp.gate_reason and "irreversible" in cp.guidance and cp.expect


# ---------------- 매칭 ----------------
def test_matching_extracts_params_from_the_masked_request(eks):
    m = match_scenario([eks], REQ, ("eks",))
    assert m is not None and m.params == {"cluster": "<CLUSTER_1>", "asg": "<ASG_1>", "to_version": "1.36", "from_version": "1.35"}
    assert m.fill("{cluster} → {to_version} {nope}") == "<CLUSTER_1> → 1.36 (미지정)"
    m2 = match_scenario([eks], "<CLUSTER_1> 클러스터를 1.36으로 업그레이드해줘", ("eks", "ec2"))
    assert m2 is not None and "asg" not in m2.params and "from_version" not in m2.params
    cmds = m2.commands()
    assert "aws eks list-insights --cluster-name <CLUSTER_1>" in cmds and not any("--auto-scaling-group-names" in c for c in cmds)      # asg가 없으면 그 조회는 건너뛴다
    assert len(cmds) == len(set(cmds)) <= 20


@pytest.mark.parametrize("text, services", [
    ("<CLUSTER_1> 클러스터에서 업그레이드 가능한 버전만 알려줘 1.36", ("eks",)),          # none_of
    ("<CLUSTER_1> 클러스터를 업그레이드해줘", ("eks",)),                                  # 목표 버전 없음(필수 param)
    ("클러스터를 1.35에서 1.36으로 업그레이드해줘", ("eks",)),                             # 클러스터 이름 없음(필수 param)
    ("<CLUSTER_1> 노드 AMI를 1.36 용으로 교체해줘", ("eks",)),                            # 업그레이드 키워드 없음
    ("<CLUSTER_1> 클러스터를 1.35에서 1.36으로 업그레이드해줘", ("ec2",)),                  # 서비스가 맞지 않음
    ("<CLUSTER_1> 클러스터 삭제해줘 1.36 업그레이드는 하지마", ("eks",)),                   # none_of(삭제)
])
def test_requests_that_do_not_fit_are_left_to_the_general_flow(eks, text, services):
    assert match_scenario([eks], text, services) is None


def test_investigation_prefers_kubectl_and_leaves_ami_and_addon_versions_to_people(eks):
    aws = [c for c in eks.investigation if c.startswith("aws ")]
    assert all(("list-insights" in c) or ("autoscaling describe-auto-scaling-groups" in c) for c in aws)           # kubectl로 볼 수 없는 것만 aws(D-44)
    assert not any("ssm get-parameter" in c or "list-addons" in c or "describe-addon" in c or "describe-cluster" in c for c in eks.investigation)
    text = " ".join(p.guidance for p in eks.phases)
    assert "조회하지 않는다" in text and "update-addon" in eks.phases[2].guidance and "직접 실행하지 않는다" in eks.phases[2].guidance


def test_node_replace_follows_the_reviewed_procedure(eks):
    node = next(p for p in eks.phases if p.id == "node-replace")
    flat = " ".join(" ".join(g) for g in node.expect)
    assert all(w in flat for w in ("iac", "cordon", "drain", "set-desired-capacity"))                            # 새 ASG(iac) → cordon → drain → 기존 ASG 축소
    assert "scale-in protection" in node.guidance and "instance-refresh" not in node.guidance


# ---------------- 시나리오가 실제 가드·문서와 맞는지 ----------------
@pytest.fixture(scope="module")
def guard(chunks, tmp_path_factory):
    snap = CliSnapshot()
    try:
        snap.build([(c.cli, c.service, c.name) for c in chunks], only_missing=True)
    except CliUnavailable:
        pytest.skip("aws/kubectl이 필요합니다")
    return CommandGuard(CommandIndex(chunks), snap, workspace=tmp_path_factory.mktemp("ws"))


def test_every_investigation_command_is_a_guarded_server_executable_read(eks, guard):
    masker = Masker(Inventory.from_file(FIXTURE))
    masker.mask("dev-main")
    masker.mask("eks-workers-a")
    m = match_scenario([eks], REQ, ("eks",))
    assert len(m.commands()) == len(eks.investigation)                                    # 샘플 값으로 모든 템플릿이 채워진다
    for cmd in m.commands():
        v = guard.check(cmd, masker)
        assert v.allow, (cmd, v.blocks)
        assert_read_only_tool_call(v)                                                      # 변경이 섞이면 여기서 걸린다
        assert_server_executable(v)
        assert v.kind == "read" and v.grounded, (cmd, v.kind, v.grounded)                                    # 문서 근거가 있는 읽기 명령


def test_phase_doc_queries_find_documents(eks, chunks):
    retriever = HybridRetriever(chunks, vector_index=None)
    for p in eks.phases:
        for q in p.docs:
            hits = retriever.search(q, k=2, method="bm25").hits
            assert hits, (p.id, q)
            assert any(q.split()[-1] in h.chunk.command or q.split()[-1] in h.chunk.text for h in hits), (p.id, q, [h.chunk.command for h in hits])


def test_missing_expectations_look_at_kind_title_and_command(eks):
    class S:
        def __init__(self, kind, title="", command=None):
            self.kind, self.title, self.command = kind, title, command
    cp = eks.phases[1]
    assert missing_expectations(cp, [S("cli", "버전 확인", "kubectl version")]) == ["scenario_missing:control-plane-upgrade:1"]          # iac step이 없다
    assert missing_expectations(cp, [S("iac", "버전 변경")]) == ["scenario_missing:control-plane-upgrade:2"]                          # kubectl 확인이 없다
    assert missing_expectations(cp, [S("iac", "버전 변경"), S("cli", "확인", "kubectl version")]) == []
    assert missing_expectations(cp, []) == ["scenario_missing:control-plane-upgrade:1", "scenario_missing:control-plane-upgrade:2"]


def test_tag_update_draft_is_recorded_but_not_active():
    """태그 수정 시나리오는 초안으로 문서에만 둔다(앱이 읽지 않는다). 파일이 규격에 맞는지만 확인한다."""
    draft = Path(__file__).parent.parent / "docs" / "scenarios-draft" / "tag-update.yaml"
    sc = parse_scenario(yaml.safe_load(draft.read_text(encoding="utf-8")), draft.name)
    assert sc.id == "tag-update" and "tag-update" not in {s.id for s in load_scenarios()}
    assert not (SCENARIO_DIR / "tag-update.yaml").exists()


# ---------------- 앱 연결 ----------------
def test_app_factory_hands_the_planner_mode_and_scenarios_to_the_agent(tmp_path, chunks, monkeypatch):
    """팩토리가 방식(`PLANNER_MODE`)과 시나리오를 에이전트에 넘기는지(넘기지 않아 batch 평가가 loop로 돈 적이 있다)."""
    from app.web.factory import build_service
    for r in ("MAIN", "SUB", "READ"):
        monkeypatch.setenv(f"{r}_MODEL", f"m-{r.lower()}")
    masker = Masker(Inventory.from_file(FIXTURE))
    for mode in ("loop", "batch"):
        monkeypatch.setenv("PLANNER_MODE", mode)
        running = build_service(data_dir=tmp_path / mode, retriever=HybridRetriever(chunks, vector_index=None))
        try:
            agent = running.service._pipeline_factory(masker, "s1")._agent
            assert agent._mode == mode and [s.id for s in agent._scenarios] == ["eks-upgrade"]
        finally:
            running.close()
