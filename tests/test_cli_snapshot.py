import os

import pytest

from app.guards.cli_snapshot import CliSnapshot, CliUnavailable, parse_aws_help, parse_kubectl_help

AWS_HELP = """
Description
***********

Creates a role.

Synopsis
********

     create-role
   --role-name <value>
   --assume-role-policy-document <value>
   [--description <value>]
   [--tags <value>]
   [--cli-input-json | --cli-input-yaml]
   [--debug]
   [--endpoint-url <value>]
   [--no-verify-ssl]
   [--output <value>]

Options
*******

"--role-name" (string)
"""

AWS_BOOL_HELP = """
Synopsis
********

     rm
   <S3Uri>
   [--dryrun]
   [--recursive]
   [--include <value>]
   [--should-decrement-desired-capacity | --no-should-decrement-desired-capacity]

Options
*******
"""

K_GET_HELP = """Get things

Options:
    -A, --all-namespaces=false:
\tIf present, list across all namespaces.

    -f, --filename=[]:
\tFilenames.

    -o, --output='':
\tOutput format.

        --raw='':
\tRaw URI.

    -w, --watch=false:
\tWatch.

Usage:
  kubectl get [(-o|--output=)json|yaml] [options]
"""

K_GROUP_HELP = """Manage rollouts

Available Commands:
  history       View rollout history
  restart       Restart a resource
  status        Show the status of the rollout

Usage:
  kubectl rollout SUBCOMMAND [options]
"""


def test_parse_aws_options_and_value_flags():
    spec = parse_aws_help(AWS_HELP)
    assert spec.options["--role-name"].takes_value and spec.options["--description"].takes_value
    assert spec.options["--debug"].takes_value is False
    assert "--endpoint-url" in spec.options and "--no-verify-ssl" in spec.options and "Options" not in spec.options


def test_parse_aws_boolean_flags_and_alternatives():
    spec = parse_aws_help(AWS_BOOL_HELP)
    assert not spec.options["--recursive"].takes_value and not spec.options["--dryrun"].takes_value
    assert not spec.options["--should-decrement-desired-capacity"].takes_value
    assert not spec.options["--no-should-decrement-desired-capacity"].takes_value
    assert spec.options["--include"].takes_value


def test_parse_kubectl_options_shorts_and_booleans():
    spec = parse_kubectl_help(K_GET_HELP)
    assert spec.options["--all-namespaces"].short == "-A" and not spec.options["--all-namespaces"].takes_value
    assert spec.options["--filename"].takes_value and spec.options["--filename"].short == "-f"
    assert spec.options["--raw"].short is None and spec.options["--raw"].takes_value
    assert not spec.options["--watch"].takes_value and spec.shorts["-w"] == "--watch"


def test_parse_kubectl_group_subcommands():
    assert parse_kubectl_help(K_GROUP_HELP).subcommands == ("history", "restart", "status")


K_TOP_HELP = """kubectl controls the Kubernetes cluster manager.

Basic Commands (Beginner):
  create          Create a resource from a file or from stdin
  run             Run a particular image on the cluster

Basic Commands (Intermediate):
  get             Display one or many resources

Subcommands provided by plugins:
  evilplugin      Provided by kubectl-evilplugin

Other Commands:
  config          Modify kubeconfig files

Usage:
  kubectl [flags] [options]
"""


def test_kubectl_builtins_exclude_plugin_provided_commands(tmp_path):
    class R:
        def __call__(self, cmd):
            return (0, K_TOP_HELP) if cmd == ["kubectl", "--help"] else (0, "x")

    b = CliSnapshot(tmp_path / "s.json", runner=R(), persist=False).kubectl_builtins()
    assert {"create", "run", "get", "config"} <= b and "evilplugin" not in b


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:2] == ["aws", "--version"]:
            return 0, "aws-cli/fake"
        if cmd[:2] == ["kubectl", "version"]:
            return 0, "Client Version: fake"
        if cmd[0] == "aws" and cmd[1:3] == ["iam", "create-role"]:
            return 0, AWS_HELP
        if cmd[0] == "aws":
            return 252, "aws: error: argument operation: Invalid choice"
        if cmd[:2] == ["kubectl", "get"]:
            return 0, K_GET_HELP
        return 1, "unknown command"


def test_get_returns_none_for_nonexistent_commands_and_caches(tmp_path):
    r = FakeRunner()
    snap = CliSnapshot(tmp_path / "s.json", runner=r)
    assert snap.get("aws", "iam", "no-such-op") is None
    n = len(r.calls)
    assert snap.get("aws", "iam", "no-such-op") is None
    assert len(r.calls) == n                       # 두 번째는 캐시


def test_get_adds_kubectl_global_namespace_option(tmp_path):
    spec = CliSnapshot(tmp_path / "s.json", runner=FakeRunner()).get("kubectl", "kubectl", "get")
    assert spec.options["--namespace"].short == "-n" and spec.options["--namespace"].takes_value


def test_snapshot_persists_and_reloads_without_running_help(tmp_path):
    path = tmp_path / "s.json"
    CliSnapshot(path, runner=FakeRunner()).get("aws", "iam", "create-role")
    r2 = FakeRunner()
    spec = CliSnapshot(path, runner=r2).get("aws", "iam", "create-role")
    assert "--role-name" in spec.options
    assert not any(c[-1] == "help" for c in r2.calls)      # 도움말 재실행 없음(버전 확인만)


def test_cache_is_discarded_when_cli_version_changes(tmp_path):
    path = tmp_path / "s.json"
    CliSnapshot(path, runner=FakeRunner()).get("aws", "iam", "create-role")

    class NewVersion(FakeRunner):
        def __call__(self, cmd):
            return (0, "aws-cli/NEWER") if cmd[:2] == ["aws", "--version"] else super().__call__(cmd)

    r = NewVersion()
    CliSnapshot(path, runner=r).get("aws", "iam", "create-role")
    assert any(c[-1] == "help" for c in r.calls)           # 다시 만들었다


def test_cli_unavailable_propagates(tmp_path):
    def boom(cmd):
        raise CliUnavailable("없음")

    with pytest.raises(CliUnavailable):
        CliSnapshot(tmp_path / "s.json", runner=boom, persist=False).get("aws", "iam", "create-role")


def test_real_snapshot_covers_every_documented_option():
    """실제 aws/kubectl로: 문서에 적은 모든 명령·옵션이 스냅샷에 있어야 한다(문서 검증기와 같은 기준을 스냅샷으로)."""
    if os.environ.get("RUN_CLI_TESTS") != "1":
        pytest.skip("RUN_CLI_TESTS=1 필요(실제 aws/kubectl 도움말을 읽는다)")
    from app.rag.chunks import load_chunks
    from app.rag.retriever import HybridRetriever  # noqa: F401  (import 확인)
    import yaml
    from app.config import KNOWLEDGE_DIR

    snap = CliSnapshot(persist=False)
    keys, documented = [], {}
    for path in KNOWLEDGE_DIR.rglob("*.md"):
        if path.name == "SCHEMA.md":
            continue
        meta = yaml.safe_load(path.read_text(encoding="utf-8").replace("\r\n", "\n").split("---\n")[1])
        for c in meta["commands"]:
            k = (meta["cli"], meta["service"], c["name"])
            keys.append(k)
            documented[k] = [*c.get("required", []), *c.get("optional", [])]
    stats = snap.build(keys)
    assert stats["missing"] == 0, stats
    for k, opts in documented.items():
        spec = snap.get(*k)
        missing = [o for o in opts if o not in spec.options]
        assert not missing, (k, missing)
