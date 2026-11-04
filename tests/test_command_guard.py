"""명령 가드 테스트. 실제 aws/kubectl 도움말 스냅샷(data/cli_snapshot.json)을 쓴다. 첫 실행은 스냅샷을 만드느라 약 40초 걸린다."""
import inspect
import os
from pathlib import Path

import pytest

from app.guards.cli_snapshot import CliSnapshot, CliUnavailable
from app.guards.command_guard import IMPLEMENTED_TAGS, CommandGuard, CommandIndex
from app.guards.masking import Inventory, Masker
from app.rag.chunks import load_chunks

FIXTURE = Path(__file__).parent / "fixtures" / "inventory.sample.yaml"
REAL = {
    "sg": "sg-0123456789abcdef0", "sg2": "sg-0fedcba9876543210", "i1": "i-0abc123def4567890", "i2": "i-0def456abc1237890",
    "acct": "123456789012", "cidr": "10.20.0.0/16",
}


@pytest.fixture(scope="session")
def chunks():
    return load_chunks()


@pytest.fixture(scope="session")
def snapshot(chunks):
    snap = CliSnapshot()
    try:
        snap.build([(c.cli, c.service, c.name) for c in chunks], only_missing=True)
    except CliUnavailable:
        pytest.skip("aws/kubectl이 필요합니다")
    return snap


@pytest.fixture()
def masker():
    return Masker(Inventory.from_file(FIXTURE))


@pytest.fixture()
def ws(tmp_path):
    (tmp_path / "data.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "ok-policy.json").write_text('{"Statement":[{"Effect":"Allow","Action":"s3:GetObject","Resource":"*"}]}', encoding="utf-8")
    (tmp_path / "wild-policy.json").write_text('{"Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}', encoding="utf-8")
    return tmp_path


@pytest.fixture()
def guard(chunks, snapshot, ws):
    return CommandGuard(CommandIndex(chunks), snapshot, workspace=ws)


def check(guard, masker, real_cmd):
    """실제 값이 든 명령을 마스킹해서(모델이 만들 형태) 가드에 넣는다."""
    return guard.check(masker.mask(real_cmd), masker)


# ---------------- 통과 ----------------
def test_documented_read_command_is_allowed_and_executable(guard, masker):
    v = check(guard, masker, f"aws ec2 describe-security-groups --group-ids {REAL['sg']}")
    assert v.allow and v.executable and v.kind == "read" and v.grounded and v.chunk_id == "aws:ec2:describe-security-groups"


def test_documented_mutating_command_is_mutate_and_needs_no_extra_when_safe(guard, masker):
    v = check(guard, masker, f"aws ec2 authorize-security-group-ingress --group-id {REAL['sg']} --protocol tcp --port 443 --cidr {REAL['cidr']}")
    assert v.allow and v.executable and v.kind == "mutate" and v.requires == () and v.flags == ("iac_drift",)


def test_kubectl_documented_with_short_options(guard, masker):
    v = check(guard, masker, "kubectl get pods -n payments -o wide")
    assert v.allow and v.kind == "read" and v.chunk_id == "kubectl:kubectl:get"


def test_kubectl_multiword_command_resolves(guard, masker):
    v = check(guard, masker, "kubectl rollout status deployment/web --namespace payments --timeout 120s")
    assert v.allow and v.chunk_id == "kubectl:kubectl:rollout status"


def test_verdict_never_contains_real_values(guard, masker):
    v = check(guard, masker, f"aws ec2 delete-security-group --group-id {REAL['sg']}")
    text = repr(v)
    assert REAL["sg"] not in text and "sg-0123" not in text


# ---------------- 구조 차단 ----------------
@pytest.mark.parametrize("cmd,code", [
    ("kubectl get pods; rm -rf /", "shell_operator"),
    ("aws ec2 describe-vpcs | cat", "shell_operator"),
    ("aws ec2 describe-vpcs > out.txt", "shell_operator"),
    ("kubectl get pods && kubectl delete ns x", "shell_operator"),
    ("kubectl get pods `whoami`", "command_substitution"),
    ("kubectl get pods $(cat /etc/passwd)", "command_substitution"),
    ("aws ec2 describe-vpcs --query $AWS_SECRET_ACCESS_KEY", "env_expansion"),
    ("aws ec2 describe-vpcs\nrm -rf /", "multiline"),
    ("terraform apply -auto-approve", "disallowed_cli"),
    ("rm -rf /", "disallowed_cli"),
    ("curl http://example.com", "disallowed_cli"),
    ("aws --region us-east-1 ec2 describe-vpcs", "global_option_before_subcommand"),
    ("kubectl -n payments get pods", "global_option_before_subcommand"),
    ("aws ec2", "operation_required"),
])
def test_structure_blocks(guard, masker, cmd, code):
    v = guard.check(cmd, masker)
    assert not v.allow and not v.executable and code in v.blocks


def test_placeholders_in_command_are_not_mistaken_for_shell_redirects(guard, masker):
    v = guard.check("aws ec2 describe-security-groups --group-ids <SG_1>", masker)
    assert "shell_operator" not in v.blocks          # <SG_1>의 < > 를 리다이렉트로 보지 않는다
    assert "unresolved_placeholder" in v.blocks       # 매핑이 없는 플레이스홀더이므로 차단


def test_unresolved_placeholder_is_blocked(guard, masker):
    masker.mask(REAL["sg"])
    v = guard.check("aws ec2 delete-security-group --group-id <SG_9>", masker)
    assert not v.allow and "unresolved_placeholder" in v.blocks


# ---------------- 범위 ----------------
@pytest.mark.parametrize("cmd", [
    "aws lambda list-functions", "aws secretsmanager get-secret-value --secret-id x",
    "aws sts get-session-token", "aws configure list", "aws rds describe-db-instances",
])
def test_services_outside_the_knowledge_base_are_blocked_even_as_fallback(guard, masker, cmd):
    v = guard.check(cmd, masker)
    assert not v.allow and "service_not_allowed" in v.blocks


@pytest.mark.parametrize("cmd,reason", [
    (f"aws ec2 get-password-data --instance-id {REAL['i1']}", "windows_password"),
    ("aws ec2 create-key-pair --key-name x", "private_key_output"),
    ("aws eks get-token --cluster-name x", "bearer_token_output"),
    ("aws iam update-login-profile --user-name x --password y", "password_input"),
    (f"aws ssm send-command --instance-ids {REAL['i1']} --document-name AWS-RunShellScript", "remote_command_execution"),
])
def test_dangerous_operations_are_denied_for_fallback(guard, masker, cmd, reason):
    v = check(guard, masker, cmd)
    assert not v.allow and f"denied_operation:{reason}" in v.blocks


def test_nonexistent_operations_and_options_are_blocked(guard, masker):
    assert "unknown_command" in guard.check("aws ec2 no-such-operation", masker).blocks
    assert "unknown_command" in guard.check("kubectl frobnicate", masker).blocks
    assert "unknown_option:--no-such" in guard.check("aws ec2 describe-vpcs --no-such x", masker).blocks
    assert "unexpected_argument" in guard.check("aws ec2 describe-vpcs stray", masker).blocks


@pytest.mark.parametrize("cmd,opt", [
    ("aws ec2 describe-vpcs --profile prod", "--profile"),
    ("aws ec2 describe-vpcs --endpoint-url http://evil.example", "--endpoint-url"),
    ("aws ec2 describe-vpcs --no-verify-ssl", "--no-verify-ssl"),
    ("aws ec2 describe-vpcs --debug", "--debug"),
    ("aws iam create-role --role-name x --cli-input-json file://x.json", "--cli-input-json"),
    ("kubectl get pods --kubeconfig /tmp/other", "--kubeconfig"),
    ("kubectl get pods --context other", "--context"),
    ("kubectl get pods --token abc", "--token"),
    ("kubectl get pods --as admin", "--as"),
    ("kubectl get --raw /api/v1/secrets", "--raw"),
])
def test_target_and_credential_options_are_forbidden(guard, masker, cmd, opt):
    v = guard.check(cmd, masker)
    assert not v.allow and f"forbidden_option:{opt}" in v.blocks


def test_forbidden_option_cannot_hide_behind_a_short_flag_cluster(guard, masker):
    assert not guard.check("kubectl get pods -owide --kubeconfig=/x", masker).allow


# ---------------- 폴백 ----------------
def test_undocumented_command_in_allowed_service_is_fallback_with_extra_approval(guard, masker):
    v = guard.check("aws ec2 describe-addresses", masker)
    assert v.allow and not v.grounded and v.kind == "mutate" and v.risk == "high"
    assert {"fallback", "unknown_impact"} <= set(v.flags) and "fallback_approval" in v.requires


def test_fallback_is_still_subject_to_every_guard(guard, masker):
    v = guard.check("aws ec2 describe-addresses --profile prod", masker)
    assert not v.allow


# ---------------- manual ----------------
def test_manual_command_is_allowed_as_text_but_never_executable(guard, masker):
    v = guard.check("kubectl exec --namespace payments --stdin --tty web-0 -- /bin/sh", masker)
    assert v.allow and v.kind == "manual" and not v.executable


def test_no_manual_command_is_ever_executable(guard, masker, chunks):
    """문서에서 manual로 지정한 모든 명령: 가드를 통과하더라도 executable은 False."""
    samples = {
        "kubectl exec": "kubectl exec web-0 -- ls", "kubectl attach": "kubectl attach web-0", "kubectl port-forward": "kubectl port-forward pod/web-0 8080:80",
        "kubectl cp": "kubectl cp payments/web-0:/a ./a", "kubectl proxy": "kubectl proxy --port 8001", "kubectl debug": "kubectl debug web-0 --image busybox",
        "kubectl edit": "kubectl edit deployment/web", "aws iam create-access-key": "aws iam create-access-key --user-name alice",
        "aws iam create-login-profile": "aws iam create-login-profile --user-name alice --password <PASSWORD>",
        "aws eks update-kubeconfig": "aws eks update-kubeconfig --name dev-main",
    }
    manual = [c for c in chunks if c.kind == "manual"]
    assert len(manual) == 10 and {c.command for c in manual} == set(samples)
    for c in manual:
        v = guard.check(samples[c.command], masker)
        assert not (v.allow and v.executable), c.command


def test_password_must_be_the_literal_placeholder(guard, masker):
    ok = guard.check("aws iam create-login-profile --user-name <USER_1> --password <PASSWORD> --password-reset-required", masker)
    assert "secret_in_command" not in ok.blocks
    bad = guard.check("aws iam create-login-profile --user-name alice --password Hunter2!", masker)
    assert not bad.allow and "secret_in_command" in bad.blocks


def test_secret_assignment_in_command_is_blocked(guard, masker):
    assert "secret_in_command" in guard.check("aws ec2 describe-vpcs --query token=abc123", masker).blocks


# ---------------- 위험 패턴 ----------------
def test_open_cidr_requires_reason_and_confirmation(guard, masker):
    v = check(guard, masker, f"aws ec2 authorize-security-group-ingress --group-id {REAL['sg']} --protocol tcp --port 443 --cidr 0.0.0.0/0")
    assert v.allow and "open_cidr" in v.flags and "open_cidr_confirm" in v.requires and v.risk == "high"


def test_open_cidr_for_any_port_gets_the_same_control(guard, masker):
    v = check(guard, masker, f"aws ec2 authorize-security-group-ingress --group-id {REAL['sg']} --protocol tcp --port 22 --cidr 0.0.0.0/0")
    assert "open_cidr_confirm" in v.requires        # 관리 포트도 같은 통제(D-09)


def test_admin_policy_attachment_is_flagged_but_readonly_is_not(guard, masker):
    admin = check(guard, masker, "aws iam attach-role-policy --role-name deploy-role --policy-arn arn:aws:iam::aws:policy/AdministratorAccess")
    ro = check(guard, masker, "aws iam attach-role-policy --role-name deploy-role --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess")
    assert "admin_policy" in admin.flags and "strong_warning" in admin.requires
    assert "admin_policy" not in ro.flags


def test_inline_trust_policy_with_wildcard_principal(guard, masker):
    pol = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":"*","Action":"sts:AssumeRole"}]}'
    v = check(guard, masker, f"aws iam create-role --role-name deploy-role --assume-role-policy-document '{pol}'")
    assert "wildcard_principal" in v.flags and "strong_warning" in v.requires


def test_policy_file_in_workspace_is_inspected_and_shown(guard, masker):
    wild = check(guard, masker, "aws iam put-role-policy --role-name deploy-role --policy-name p --policy-document file://wild-policy.json")
    ok = check(guard, masker, "aws iam put-role-policy --role-name deploy-role --policy-name p --policy-document file://ok-policy.json")
    assert "wildcard_action" in wild.flags and "wild-policy.json" in wild.local_paths[0] and "show_file_contents" in wild.requires
    assert "wildcard_action" not in ok.flags and "show_file_contents" in ok.requires


def test_unreadable_policy_file_is_flagged_not_trusted(guard, masker):
    v = check(guard, masker, "aws iam put-role-policy --role-name deploy-role --policy-name p --policy-document file://missing.json")
    assert "policy_unreadable" in v.flags and "strong_warning" in v.requires


def test_s3_recursive_delete_and_sync_delete_need_a_dry_run_first(guard, masker):
    rm = check(guard, masker, "aws s3 rm s3://acme-logs/logs/ --recursive")
    sync = check(guard, masker, "aws s3 sync s3://acme-logs/a s3://acme-artifacts/a --delete")
    rb = check(guard, masker, "aws s3 rb s3://acme-logs --force")
    for v, f in ((rm, "recursive_delete"), (sync, "sync_delete"), (rb, "recursive_delete")):
        assert f in v.flags and "dry_run_first" in v.requires and "strong_warning" in v.requires


def test_s3_public_exposure_is_flagged(guard, masker):
    relax = check(guard, masker, "aws s3api put-public-access-block --bucket acme-logs --public-access-block-configuration BlockPublicAcls=false,IgnorePublicAcls=true")
    acl = check(guard, masker, "aws s3api put-bucket-acl --bucket acme-logs --acl public-read")
    private = check(guard, masker, "aws s3api put-bucket-acl --bucket acme-logs --acl private")
    assert "public_access_relax" in relax.flags and "public_acl" in acl.flags and "public_acl" not in private.flags


def test_multiple_targets_are_flagged(guard, masker):
    one = check(guard, masker, f"aws ec2 terminate-instances --instance-ids {REAL['i1']}")
    many = check(guard, masker, f"aws ec2 terminate-instances --instance-ids {REAL['i1']} {REAL['i2']}")
    assert "multiple_targets" not in one.flags and "multiple_targets" in many.flags
    asg = check(guard, masker, "aws autoscaling terminate-instance-in-auto-scaling-group --auto-scaling-group-name eks-workers-a --no-should-decrement-desired-capacity")
    assert "multiple_targets" in asg.flags


def test_prod_target_comes_from_the_server_rule_not_the_model(guard, masker):
    v = check(guard, masker, "aws s3 ls s3://acme-artifacts/")
    dev = check(guard, masker, "aws s3 ls s3://acme-logs/")
    assert "prod_target" in v.flags and "prod_warning" in v.requires and "prod_target" not in dev.flags


# ---------------- kubectl 특화 ----------------
@pytest.mark.parametrize("cmd,code", [
    ("kubectl get pods -w", "blocking_option"),
    ("kubectl get pods --watch", "blocking_option"),
    ("kubectl logs web-0 -f", "blocking_option"),
    ("kubectl logs web-0 --follow", "blocking_option"),
    ("kubectl rollout status deployment/web", "timeout_required"),
    ("kubectl drain node-1 --ignore-daemonsets", "timeout_required"),
    ("kubectl get secret db -o yaml", "secret_output"),
    ("kubectl get secrets --namespace payments -o json", "secret_output"),
    ("kubectl get pods,secrets -o jsonpath={.items}", "secret_output"),
    ("kubectl create secret generic db --from-literal=pw=x", "secret_creation"),
    ("kubectl delete pods -A", "delete_all_namespaces"),
    ("kubectl delete", "target_missing"),
])
def test_kubectl_blocks(guard, masker, cmd, code):
    v = guard.check(cmd, masker)
    assert not v.allow and code in v.blocks


def test_kubectl_safe_variants_pass(guard, masker):
    assert guard.check("kubectl get secrets --namespace payments", masker).allow            # 값이 아닌 목록
    assert guard.check("kubectl describe secret db --namespace payments", masker).allow     # 값은 보여주지 않는다
    assert guard.check("kubectl logs web-0 --tail 100", masker).allow
    assert guard.check("kubectl rollout status deployment/web --timeout=60s", masker).allow
    assert guard.check("kubectl drain node-1 --ignore-daemonsets --timeout 300s --dry-run=server", masker).allow


def test_kubectl_delete_all_and_namespace_are_strongly_warned(guard, masker):
    a = check(guard, masker, "kubectl delete pods --all --namespace payments")
    n = check(guard, masker, "kubectl delete namespace payments")
    assert "delete_all" in a.flags and "strong_warning" in a.requires
    assert "namespace_delete" in n.flags and "strong_warning" in n.requires
    z = check(guard, masker, "kubectl scale deployment/web --replicas=0 --namespace payments")
    assert "scale_to_zero" in z.flags and "strong_warning" in z.requires


# ---------------- 우회 시도(레드팀 회귀 테스트) ----------------
@pytest.mark.parametrize("principal", ['"*"', '{"AWS": "*"}', '{"Service": "*"}', '{"Federated": "*"}', '{"AWS": ["arn:aws:iam::<ACCT>:root", "*"]}'])
def test_wildcard_principal_is_caught_under_every_principal_key(guard, masker, principal):
    pol = '{"Statement":[{"Effect":"Allow","Principal":' + principal + ',"Action":"sts:AssumeRole"}]}'
    v = check(guard, masker, f"aws iam create-role --role-name deploy-role --assume-role-policy-document '{pol}'")
    assert "wildcard_principal" in v.flags


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "0.0.0.0/00", "0.0.0.0/000"])
def test_open_cidr_spellings(guard, masker, cidr):
    v = check(guard, masker, f"aws ec2 authorize-security-group-ingress --group-id {REAL['sg']} --protocol tcp --port 443 --cidr {cidr}")
    assert "open_cidr" in v.flags and "open_cidr_confirm" in v.requires


def test_internet_halves_and_other_very_wide_ranges_are_flagged(guard, masker):
    for cidr in ("0.0.0.0/1", "128.0.0.0/1", "64.0.0.0/2", "0.0.0.0/7"):
        v = check(guard, masker, f"aws ec2 authorize-security-group-ingress --group-id {REAL['sg']} --protocol tcp --port 443 --cidr {cidr}")
        assert "wide_cidr" in v.flags and "strong_warning" in v.requires, cidr
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "10.1.0.0/16", "192.168.4.0/24"):
        v = check(guard, masker, f"aws ec2 authorize-security-group-ingress --group-id {REAL['sg']} --protocol tcp --port 443 --cidr {cidr}")
        assert "wide_cidr" not in v.flags and "open_cidr" not in v.flags, cidr


def test_public_wide_ranges_flagged_but_narrow_ones_are_not(guard, masker):
    for cidr, wide in (("8.0.0.0/8", True), ("52.10.0.0/16", True), ("52.10.4.0/24", False), ("203.0.113.0/24", False)):
        v = check(guard, masker, f"aws ec2 authorize-security-group-ingress --group-id {REAL['sg']} --protocol tcp --port 443 --cidr {cidr}")
        assert ("wide_cidr" in v.flags) is wide, cidr


def test_kubeconfig_mutation_is_blocked(guard, masker):
    for sub in ("use-context dev-main", "set-context --current --namespace=payments", "set-cluster x --server=https://x",
                "set-credentials u --token=x", "unset users.u", "delete-context dev-main", "rename-context a b"):
        v = guard.check(f"kubectl config {sub}", masker)
        assert not v.allow, sub
    assert "kubeconfig_mutation" not in guard.check("kubectl config get-contexts", masker).blocks


def test_interactive_options_are_blocked_unless_the_command_is_manual(guard, masker):
    for cmd in ("kubectl run tmp --image busybox --stdin --tty", "kubectl run tmp --image busybox -it"):
        v = guard.check(cmd, masker)
        assert not v.allow and "interactive_option" in v.blocks, cmd
    assert guard.check("kubectl exec web-0 --stdin --tty -- /bin/sh", masker).kind == "manual"   # manual은 허용(서버는 실행하지 않음)


def test_secret_values_cannot_be_read_through_a_manifest_file(guard, masker, ws):
    (ws / "s.yaml").write_text("kind: Secret", encoding="utf-8")
    for cmd in ("kubectl get --filename s.yaml -o yaml", "kubectl get -f s.yaml -o json", "kubectl get -f s.yaml -o jsonpath={.data}"):
        v = guard.check(cmd, masker)
        assert not v.allow and "secret_output_unverifiable" in v.blocks, cmd
    assert guard.check("kubectl get -f s.yaml", masker).allow       # 값을 보여주지 않는 기본 출력은 통과


def test_unknown_kubectl_names_never_reach_help_so_plugins_cannot_run(chunks, masker):
    calls = []

    def runner(cmd):
        calls.append(cmd)
        if cmd[:2] == ["kubectl", "--help"]:
            return 0, "Other Commands:\n  config          Modify kubeconfig files\n\nSubcommands provided by plugins:\n  evilplugin      x\n\nUsage:\n"
        return (0, "ok") if cmd[:2] in (["aws", "--version"], ["kubectl", "version"]) else (0, "Options:\n\nUsage:\n")

    g = CommandGuard(CommandIndex(chunks), CliSnapshot(persist=False, runner=runner))
    for name in ("evilplugin", "not-a-real-command"):
        v = g.check(f"kubectl {name} arg", masker)
        assert not v.allow and "unknown_command" in v.blocks
    assert ["kubectl", "evilplugin", "--help"] not in calls           # 플러그인은 실행되지 않았다


def test_abbreviated_or_inline_forbidden_options_do_not_slip_through(guard, masker):
    for cmd in ("aws ec2 describe-vpcs --endpoint http://x", "aws ec2 describe-vpcs --endpoint-url=http://x", "aws ec2 describe-vpcs --prof prod",
                "kubectl get pods --kubeconfig=/x", "kubectl get pods --kube /x", "kubectl get pods -s http://x"):
        assert not guard.check(cmd, masker).allow, cmd


def test_command_prefix_and_lookalike_tricks_are_rejected(guard, masker):
    for cmd in ("KUBECONFIG=/x kubectl get pods", "env aws ec2 describe-vpcs", "aws ec2 describe-vpcs --query 'x' ; id",
                "аws ec2 describe-vpcs", "'aws ec2' describe-vpcs", "sudo aws ec2 describe-vpcs"):
        assert not guard.check(cmd, masker).allow, cmd


# ---------------- ssm ----------------
def test_ssm_only_reads_public_aws_service_parameters(guard, masker):
    ok = guard.check("aws ssm get-parameter --name /aws/service/eks/optimized-ami/1.36/amazon-linux-2023/x86_64/standard/recommended/image_id --query Parameter.Value --output text", masker)
    other = guard.check("aws ssm get-parameter --name /prod/db/password", masker)
    dec = guard.check("aws ssm get-parameter --name /aws/service/x --with-decryption", masker)
    assert ok.allow and not other.allow and "ssm_scope" in other.blocks and "ssm_decryption" in dec.blocks


# ---------------- 로컬 경로 샌드박스 ----------------
@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "C:\\Windows\\win.ini", "~/.aws/credentials", "%USERPROFILE%\\x", "$HOME/x", "-", "http://example.com/x.yaml"])
def test_paths_outside_the_workspace_are_blocked(guard, masker, path):
    for cmd in (f"aws s3 cp '{path}' s3://acme-logs/x", f"kubectl apply --filename '{path}'"):
        v = check(guard, masker, cmd)
        # $HOME, %VAR% 는 환경변수 확장으로 더 일찍 막히므로 둘 중 하나의 사유면 된다
        assert not v.allow and {"local_path_outside_workspace", "env_expansion"} & set(v.blocks), (cmd, v.blocks)


def test_file_uri_outside_workspace_is_blocked(guard, masker):
    v = guard.check("aws iam create-policy --policy-name p --policy-document file://../../secret.json", masker)
    assert not v.allow and "local_path_outside_workspace" in v.blocks


def test_workspace_paths_pass_and_are_listed_for_display(guard, masker):
    v = check(guard, masker, "aws s3 cp data.txt s3://acme-logs/data.txt")
    assert v.allow and v.local_paths == ("data.txt",) and "show_file_contents" in v.requires


def test_no_workspace_means_no_local_paths_at_all(chunks, snapshot, masker):
    g = CommandGuard(CommandIndex(chunks), snapshot, workspace=None)
    assert not check(g, masker, "aws s3 cp data.txt s3://acme-logs/data.txt").allow
    assert check(g, masker, "aws s3 ls s3://acme-logs/").allow


# ---------------- fail-closed, 우회 불가 ----------------
def test_guard_errors_become_blocks(chunks, snapshot, masker):
    class Exploding:
        def get(self, *a, **k):
            raise RuntimeError("boom")

    v = CommandGuard(CommandIndex(chunks), Exploding()).check("aws ec2 describe-vpcs", masker)
    assert not v.allow and "guard_error" in v.blocks


def test_unavailable_cli_blocks_instead_of_passing(chunks, masker):
    def boom(cmd):
        raise CliUnavailable("없음")

    snap = CliSnapshot(persist=False, runner=boom)
    v = CommandGuard(CommandIndex(chunks), snap).check("aws ec2 describe-vpcs", masker)
    assert not v.allow and "cli_unavailable" in v.blocks


def test_non_string_and_empty_commands_are_blocked(guard, masker):
    for bad in (None, 123, "", "   "):
        assert not guard.check(bad, masker).allow


def test_there_is_no_way_to_switch_the_guard_off(guard, masker, chunks, snapshot, monkeypatch):
    forbidden = {"enabled", "disable", "disabled", "skip", "bypass", "strict", "dry_run", "force", "unsafe", "debug", "off"}
    params = set(inspect.signature(CommandGuard.check).parameters) | set(inspect.signature(CommandGuard.__init__).parameters)
    assert not (params & forbidden), params & forbidden
    for var in ("GUARD_DISABLED", "DISABLE_GUARDS", "SKIP_GUARDS", "GUARDS_OFF", "UNSAFE", "DEBUG"):
        monkeypatch.setenv(var, "1")
    assert not guard.check("aws ec2 describe-vpcs --profile prod", masker).allow
    assert not guard.check("kubectl get pods; rm -rf /", masker).allow


def test_documented_guard_tags_are_all_implemented(chunks):
    """문서가 약속한 보호(guard 태그)는 가드에 구현되어 있어야 한다."""
    used = {t for c in chunks for t in c.guard}
    assert used <= IMPLEMENTED_TAGS, used - IMPLEMENTED_TAGS
