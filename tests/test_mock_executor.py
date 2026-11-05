"""mock 실행기: moto 서버, 시뮬레이터(EKS 업그레이드·인스턴스 리프레시), kubectl 고정 응답, 실제 AWS로 나가지 않음.

aws CLI와 moto가 필요하다(없으면 건너뛴다). moto 서버 하나를 모듈 안에서 공유하고, 테스트마다 새 실행기(새 시뮬레이션 상태)를 쓴다.
"""
import json
import shutil
from pathlib import Path

import pytest

from app.agents.tools import ReadToolbox
from app.executor.adapter import ReadRunnerAdapter
from app.executor.base import ExecutionRequest
from app.executor.mock import MockExecutor
from app.executor.moto_server import MotoServer
from app.guards.agent_limits import AgentBudget
from app.guards.cli_snapshot import CliSnapshot, CliUnavailable
from app.guards.command_guard import CommandGuard, CommandIndex
from app.guards.execution_policy import ExecutionDenied
from app.guards.masking import Inventory, Masker
from app.rag.chunks import load_chunks
from app.rag.retriever import HybridRetriever

FIXTURE = Path(__file__).parent / "fixtures" / "inventory.sample.yaml"

pytestmark = pytest.mark.skipif(shutil.which("aws") is None, reason="aws CLI가 필요합니다")


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


@pytest.fixture(scope="module")
def moto():
    server = MotoServer()
    try:
        server.start()
    except RuntimeError:
        pytest.skip("moto 서버를 띄울 수 없습니다")
    yield server
    server.close()


@pytest.fixture(scope="module")
def guard(chunks, snapshot, tmp_path_factory):
    return CommandGuard(CommandIndex(chunks), snapshot, workspace=tmp_path_factory.mktemp("ws"))


@pytest.fixture(scope="module")
def seeded(moto, guard, tmp_path_factory):
    ex = MockExecutor(guard, tmp_path_factory.mktemp("seed"), moto=moto)
    return ex.seed()


@pytest.fixture()
def ex(moto, guard, seeded, tmp_path):
    return MockExecutor(guard, tmp_path, moto=moto)


def run(ex, real, *, approved=False, confirmations=()):
    v = ex._guard.check(real)
    req = ExecutionRequest.create(kind=v.kind, masked_command=real, command=real)
    return ex.execute(req, verdict=v, approved_by_other=approved, confirmations=set(confirmations))


def js(res):
    assert res.status == "ok", res.stderr
    return json.loads(res.stdout)


def test_moto_reads_run_in_mock_and_are_marked(ex, moto):
    r = run(ex, "aws eks describe-cluster --name dev-main")
    assert r.mock and r.executor == "mock" and "moto" in r.notes and js(r)["cluster"]["version"] == "1.35"
    assert moto.endpoint.startswith("http://127.0.0.1:")
    assert "acme-logs" in run(ex, "aws s3 ls").stdout


def test_a_missing_resource_fails_with_the_error_text_and_exit_code(ex):
    r = run(ex, "aws eks describe-cluster --name no-such-cluster")
    assert r.status == "failed" and r.exit_code != 0 and "ResourceNotFound" in r.stderr


def test_security_group_rule_roundtrip_in_moto(ex):
    vpc = js(run(ex, "aws ec2 describe-vpcs"))["Vpcs"][0]["VpcId"]
    sg = js(run(ex, f"aws ec2 create-security-group --group-name probe-sg-{id(ex)} --description probe --vpc-id {vpc}", approved=True))["GroupId"]
    run(ex, f"aws ec2 authorize-security-group-ingress --group-id {sg} --protocol tcp --port 443 --cidr 10.20.0.0/16", approved=True)
    rules = js(run(ex, f"aws ec2 describe-security-groups --group-ids {sg}"))["SecurityGroups"][0]["IpPermissions"]
    assert any(p.get("FromPort") == 443 for p in rules)


def test_mutating_commands_still_need_approval_in_mock(ex):
    with pytest.raises(ExecutionDenied):
        run(ex, "aws ec2 reboot-instances --instance-ids i-0abc123def4567890")


def test_eks_upgrade_simulation_progresses_by_polling_and_updates_the_world(ex):
    assert js(run(ex, "aws eks describe-cluster --name dev-main"))["cluster"]["status"] == "ACTIVE"
    r = run(ex, "aws eks update-cluster-version --name dev-main --kubernetes-version 1.36", approved=True)
    assert "simulator" in r.notes and js(r)["update"]["status"] == "InProgress"
    seen = []
    for _ in range(4):
        c = js(run(ex, "aws eks describe-cluster --name dev-main"))["cluster"]
        seen.append((c["status"], c["version"]))
    assert seen[0][0] == "UPDATING" and seen[-1] == ("ACTIVE", "1.36")
    assert ex.world.control_plane_version == "1.36"
    assert "v1.36" in run(ex, "kubectl version").stdout


def test_instance_refresh_simulation_replaces_nodes_with_the_new_version(ex):
    ex.world.control_plane_version = "1.36"                               # 컨트롤 플레인은 이미 올라간 상태
    assert js(run(ex, "aws autoscaling start-instance-refresh --auto-scaling-group-name eks-workers-a --strategy Rolling", approved=True))["InstanceRefreshId"]
    assert "v1.35" in run(ex, "kubectl get nodes").stdout
    statuses = [js(run(ex, "aws autoscaling describe-instance-refreshes --auto-scaling-group-name eks-workers-a"))["InstanceRefreshes"][0]["Status"] for _ in range(3)]
    assert statuses[0] == "Pending" and statuses[1] == "InProgress" and statuses[-1] == "Successful"
    assert "v1.36" in run(ex, "kubectl get nodes").stdout and "v1.35" not in run(ex, "kubectl get nodes").stdout


def test_static_eks_reads_are_simulated(ex):
    assert js(run(ex, "aws eks list-addons --cluster-name dev-main"))["addons"] == ["coredns", "kube-proxy", "vpc-cni"]
    assert js(run(ex, "aws eks list-insights --cluster-name dev-main"))["insights"][0]["category"] == "UPGRADE_READINESS"
    versions = js(run(ex, "aws eks describe-addon-versions --kubernetes-version 1.36 --addon-name coredns"))
    assert versions["addons"][0]["addonVersions"][0]["compatibilities"][0]["clusterVersion"] == "1.36"


def test_kubectl_fixture_reads_and_node_isolation_state(ex):
    out = run(ex, "kubectl get nodes --output wide").stdout
    assert "worker-a1" in out and "10.0.1.10" in out and "Ready" in out
    assert "SchedulingDisabled" not in out
    assert "cordoned" in run(ex, "kubectl cordon worker-a1", approved=True).stdout
    assert "SchedulingDisabled" in run(ex, "kubectl get nodes").stdout
    run(ex, "kubectl uncordon worker-a1", approved=True)
    assert "SchedulingDisabled" not in run(ex, "kubectl get nodes").stdout
    assert "payments" in run(ex, "kubectl get pods --all-namespaces").stdout and "web-0" in run(ex, "kubectl get pods --namespace payments").stdout
    assert "CPU" in run(ex, "kubectl top node").stdout and run(ex, "kubectl config current-context").stdout.strip() == "mock-context"
    assert "scaled" in run(ex, "kubectl scale deployment/web --replicas=3 --namespace payments", approved=True).stdout


def test_kubectl_fixture_never_pretends_to_support_the_unknown(ex):
    r = run(ex, "kubectl get widgets")
    assert r.status == "failed" and r.exit_code == 1 and "resource type" in r.stderr
    n = run(ex, "kubectl cordon no-such-node", approved=True)
    assert n.status == "failed" and "NotFound" in n.stderr


def test_server_environment_never_reaches_the_aws_cli(ex, monkeypatch):
    """서버 환경에 실제 키·프로필·다른 엔드포인트가 있어도 mock 실행은 그것을 쓰지 않는다(쓰면 프로필 오류나 연결 오류가 난다)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAREALLOOKINGKEY00")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "realsecret")
    monkeypatch.setenv("AWS_PROFILE", "profile-that-does-not-exist")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://real.example.invalid")
    r = run(ex, "aws iam list-users")
    assert r.status == "ok" and "users" in r.stdout.lower()


def test_agent_read_tool_runs_on_the_mock_and_the_output_is_masked(guard, chunks, ex):
    masker = Masker(Inventory.from_file(FIXTURE))
    tb = ReadToolbox(masker=masker, guard=guard, retriever=HybridRetriever(chunks, vector_index=None),
                     runner=ReadRunnerAdapter(ex, guard, masker), budget=AgentBudget(), search_kwargs={"method": "bm25"})
    r = tb.call("run_read_command", {"command": "aws ec2 describe-vpcs"})
    assert r["ok"] and "<untrusted_data" in r["output"] and "vpc-" not in r["output"].replace("<VPC_", "")      # 리소스 ID는 마스킹된다
    bad = tb.call("run_read_command", {"command": "aws ec2 describe-vpcs --profile prod"})
    assert bad["ok"] is False
    miss = tb.call("run_read_command", {"command": "aws eks describe-cluster --name no-such-cluster"})
    assert miss["ok"] and "ResourceNotFound" in miss["output"]                           # 실패 원인은 마스킹된 오류 출력으로 돌아온다


def test_public_ami_parameters_for_ubuntu_and_amazon_linux_resolve_in_the_mock(ex):
    """실측: EC2 생성 계획에서 모델이 AMI를 찾지 못해 없는 플레이스홀더를 지어냈다. 공개 SSM 파라미터는 mock에서 합성 AMI ID를 돌려준다."""
    for name in ("/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id",
                 "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"):
        r = js(run(ex, f"aws ssm get-parameter --name {name}"))
        assert r["Parameter"]["Name"] == name and r["Parameter"]["Value"].startswith("ami-")
    other = run(ex, "aws ssm get-parameter --name /aws/service/unknown/x")
    assert other.status == "failed"                                                      # 모르는 공개 경로는 여전히 moto가 "없음"으로 답한다
