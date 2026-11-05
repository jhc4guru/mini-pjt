"""명령 가드(출력 가드): 모델이 만든 명령, 또는 사용자가 직접 넣은 명령을 검사한다.

원칙
- 하나라도 확신할 수 없으면 차단한다(fail-closed). 예상치 못한 오류도 차단이다.
- 검사는 마스킹된 명령(플레이스홀더)을 받아 서버 안에서 복원한 값으로 한다. 위험 판단(ARN의 정책 이름, prod 이름 등)에 실제 값이 필요하기 때문이다.
  복원한 값은 반환값에 담지 않는다.
- 문서에 없는 명령(폴백)도 이 가드를 똑같이 통과해야 한다. 허용 CLI·서비스 범위와 manual 거부, 옵션 검증이 폴백에도 적용된다.
- 이 가드를 끄거나 완화하는 인자·환경변수는 없다.
"""
from __future__ import annotations

import ipaddress
import json
import re
import shlex
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

from app.guards.cli_snapshot import CliSnapshot, CliUnavailable, CommandSpec
from app.guards.masking import LITERAL_PLACEHOLDERS, PLACEHOLDER_RE, Masker
from app.guards.patterns import find_secrets
from app.guards.refs import find_refs, has_unparsed_ref, with_dummy
from app.rag.chunks import Chunk

ALLOWED_CLIS = frozenset({"aws", "kubectl"})
MAX_COMMAND_CHARS = 4000
MAX_POLICY_BYTES = 64 * 1024

# 실행 대상·자격증명·인증 경로를 바꾸거나 숨은 입력을 읽는 옵션은 모델이 쓸 수 없다.
# (mock 모드의 엔드포인트와 더미 키는 환경변수로 강제한다. 옵션으로 덮어쓰면 실제 AWS로 나갈 수 있다.)
AWS_FORBIDDEN_OPTS = frozenset({
    "--profile", "--endpoint-url", "--no-verify-ssl", "--ca-bundle", "--no-sign-request", "--debug",
    "--cli-input-json", "--cli-input-yaml", "--generate-cli-skeleton", "--cli-auto-prompt",
})
KUBECTL_FORBIDDEN_OPTS = frozenset({
    "--kubeconfig", "--context", "--cluster", "--user", "--server", "--token", "--as", "--as-group", "--as-uid",
    "--insecure-skip-tls-verify", "--certificate-authority", "--client-certificate", "--client-key",
    "--username", "--password", "--tls-server-name", "--cache-dir", "--raw", "--v", "--vmodule", "--profile",
    "--profile-output",
})

# 문서에 없어도(폴백) 만들 수 없는 AWS 작업: 비밀값을 만들어 내거나, 인스턴스에서 임의 명령을 실행한다.
DENY_OPERATIONS: dict[tuple[str, str], str] = {
    ("ec2", "get-password-data"): "windows_password",
    ("ec2", "create-key-pair"): "private_key_output",
    ("eks", "get-token"): "bearer_token_output",
    ("iam", "update-login-profile"): "password_input",
    ("iam", "change-password"): "password_input",
    ("iam", "create-service-specific-credential"): "credential_output",
    ("iam", "reset-service-specific-credential"): "credential_output",
    ("ssm", "send-command"): "remote_command_execution",
    ("ssm", "start-session"): "interactive_session",
    ("ssm", "start-automation-execution"): "remote_command_execution",
}
# 값이 고정 자리표시(<PASSWORD>)여야만 하는 옵션
SECRET_VALUE_OPTS = frozenset({"--password"})

TIMEOUT_REQUIRED = frozenset({"rollout status", "drain", "wait"})
BLOCKING_OPTS = frozenset({"--watch", "--watch-only", "--follow"})
REVEALING_OUTPUT = re.compile(r"^(?:yaml|json|jsonpath|go-template|template|custom-columns|jsonpath-as-json)", re.I)
POLICY_OPTS = frozenset({"--policy", "--policy-document", "--assume-role-policy-document"})
ADMIN_POLICY = re.compile(
    r"arn:aws[a-z-]*:iam::aws:policy/(?:[A-Za-z0-9+=,.@_/-]*/)?(?:AdministratorAccess|PowerUserAccess|IAMFullAccess|[A-Za-z0-9+=,.@_-]*FullAccess)\b"
)
OPEN_CIDR = re.compile(r"(?<![\d.:])(?:0\.0\.0\.0/0+|::/0+)(?!\d)")     # 0.0.0.0/00 같은 표기도 같은 전체 개방이다
WIDE_CIDR = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})(?!\d)")  # /7 이하는 사실상 인터넷 절반 이상(0.0.0.0/1 + 128.0.0.0/1로 쪼개 우회 가능)
_PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
# 서버 전역 kubeconfig의 현재 context/자격증명을 바꾸는 하위 명령(D-23). 실행기가 요청별 격리 kubeconfig를 쓰더라도 계획에는 넣지 않는다.
MUTATING_KUBECONFIG = frozenset({
    "use-context", "set-context", "set-cluster", "set-credentials", "set", "unset",
    "delete-context", "delete-cluster", "delete-user", "rename-context",
})


def _is_wide_cidr(addr: str, prefix: int) -> bool:
    """/7 이하는 항상 넓다. 그 밖에는 사설(RFC1918) 대역이 아닌 /16 이하(공인 /16 이상 범위)를 넓다고 본다."""
    if prefix <= 7:
        return True
    try:
        net = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
    except ValueError:
        return False
    if any(net.subnet_of(p) for p in _PRIVATE_NETS):
        return False
    return prefix <= 16


INTERACTIVE_OPTS = ("--stdin", "--tty")
SHELL_OPERATOR = re.compile(r"^[;&|<>()]+$")
PATH_OPTS_KUBECTL = frozenset({"--filename", "--kustomize", "--from-file", "--from-env-file", "--patch-file"})

# 문서(knowledge/cli)의 guard 태그 중 이 가드가 실제로 구현한 것. 문서가 없는 보호를 약속하지 못하게 테스트가 대조한다.
IMPLEMENTED_TAGS = frozenset({
    "open-cidr", "wildcard-principal", "wildcard-action", "admin-policy", "recursive-delete", "sync-delete",
    "public-access", "output-secret", "input-secret", "local-file", "no-follow", "delete-all", "file-input",
})

HIGH_FLAGS = frozenset({
    "open_cidr", "wildcard_principal", "wildcard_action", "admin_policy", "recursive_delete", "sync_delete",
    "delete_all", "namespace_delete", "public_access_relax", "public_acl", "prod_target", "policy_unreadable",
    "user_data_script", "unknown_impact", "negated_element", "multiple_targets", "wide_cidr",
})
_STRONG = HIGH_FLAGS | {"scale_to_zero"}


@dataclass(frozen=True)
class CommandVerdict:
    allow: bool                       # False면 계획에 넣을 수 없다
    executable: bool                  # 서버 실행기가 실행해도 되는가(allow이고 manual이 아님)
    command: str                      # 입력 그대로(마스킹된 명령)
    kind: str = ""                    # read | mutate | manual
    risk: str = "high"
    grounded: bool = False            # 문서 근거가 있는가
    chunk_id: str | None = None
    blocks: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()    # 승인 단계에서 필요한 추가 조치
    impact: dict = field(default_factory=dict, compare=False, hash=False)
    local_paths: tuple[str, ...] = ()  # 샌드박스를 통과한 로컬 경로(승인 화면에 내용을 보여줄 대상)


def _blocked(command: str, *codes: str) -> CommandVerdict:
    return CommandVerdict(allow=False, executable=False, command=command, blocks=tuple(dict.fromkeys(codes)))


class CommandIndex:
    """문서(chunk)로 만든 명령 색인."""

    def __init__(self, chunks: list[Chunk]):
        self.by_key = {(c.cli, c.service, c.name): c for c in chunks}
        self.aws_services = frozenset(c.service for c in chunks if c.cli == "aws")
        self._kubectl = sorted((c for c in chunks if c.cli == "kubectl"), key=lambda c: len(c.name.split()), reverse=True)

    def aws(self, service: str, op: str) -> Chunk | None:
        return self.by_key.get(("aws", service, op))

    def kubectl(self, words: list[str]) -> Chunk | None:
        for c in self._kubectl:
            n = c.name.split()
            if words[: len(n)] == n:
                return c
        return None


# ---------------- 파싱 ----------------
def _protect(text: str) -> str:
    """플레이스홀더의 < > 가 셸 연산자로 읽히지 않게 임시 토큰으로 바꾼다."""
    text = PLACEHOLDER_RE.sub(lambda m: f"__PH_{m.group(1)}_{m.group(2)}__", text)
    for lit in LITERAL_PLACEHOLDERS:
        text = text.replace(lit, "__PH_" + lit.strip("<>") + "__")
    return text


_UNPROTECT = re.compile(r"__PH_([A-Z][A-Z_]*?)(?:_(\d+))?__")


def _unprotect(tok: str) -> str:
    return _UNPROTECT.sub(lambda m: f"<{m.group(1)}_{m.group(2)}>" if m.group(2) else f"<{m.group(1)}>", tok)


def _split(command: str) -> tuple[list[str], list[str]]:
    """(토큰, 차단 코드). 셸 연산자·치환·환경변수 확장은 막는다. 실행기는 셸을 쓰지 않지만 의도가 불명확한 명령이다."""
    blocks: list[str] = []
    if "\n" in command or "\r" in command:
        return [], ["multiline"]
    if len(command) > MAX_COMMAND_CHARS:
        return [], ["too_long"]
    if "`" in command or "$(" in command:      # 토큰으로 쪼개기 전에 원문에서 먼저 본다(정확한 사유를 남기기 위해)
        blocks.append("command_substitution")
    lex = shlex.shlex(_protect(command), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        return [], ["unbalanced_quotes"]
    for t in tokens:
        if SHELL_OPERATOR.match(t):
            blocks.append("shell_operator")
        if "`" in t or "$(" in t:
            blocks.append("command_substitution")
        json_like = t[:1] in "{[" or '"' in t
        if (re.search(r"\$[A-Za-z_{]", t) and not json_like) or re.search(r"%[A-Za-z_]+%", t):
            blocks.append("env_expansion")
    return [_unprotect(t) for t in tokens], list(dict.fromkeys(blocks))


@dataclass
class _Parsed:
    opts: list[tuple[str, list[str]]] = field(default_factory=list)
    positionals: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def values(self, long: str) -> list[str]:
        return [v for name, vals in self.opts if name == long for v in vals]

    def has(self, long: str) -> bool:
        return any(name == long for name, _ in self.opts)

    def value(self, long: str) -> str | None:
        v = self.values(long)
        return v[0] if v else None


def _parse_args(args: list[str], spec: CommandSpec, *, greedy: bool) -> _Parsed:
    """greedy: 값 옵션 뒤의 다음 옵션 전까지 모든 토큰을 그 옵션의 값으로 본다(aws 리스트 값, 위치 인자 없는 명령).
    greedy가 아니면 값 옵션은 토큰 하나만 가져간다(kubectl, aws s3의 위치 인자)."""
    p, shorts, i = _Parsed(), spec.shorts, 0

    def take(opt, inline: str | None) -> None:
        nonlocal i
        if inline is not None:
            p.opts.append((opt.long, [inline]))
        elif opt.takes_value:
            vals: list[str] = []
            while i + 1 < len(args) and (not args[i + 1].startswith("-") or re.fullmatch(r"-\d+", args[i + 1])):
                vals.append(args[i + 1])
                i += 1
                if not greedy:
                    break
            if not vals and i + 1 < len(args) and not greedy:       # 값이 '-'로 시작하는 경우(예: --tail -1)
                vals.append(args[i + 1])
                i += 1
            p.opts.append((opt.long, vals))
        else:
            p.opts.append((opt.long, []))

    while i < len(args):
        a = args[i]
        if a == "--":
            p.positionals.extend(args[i + 1:])
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            opt = spec.options.get(name)
            if opt is None:
                p.unknown.append(name)
            else:
                take(opt, val if eq else None)
        elif a.startswith("-") and len(a) >= 2 and not re.fullmatch(r"-\d+", a):
            body, eq, val = a[1:].partition("=")
            for j, ch in enumerate(body):
                long = shorts.get(f"-{ch}")
                if long is None:
                    p.unknown.append(f"-{ch}")
                    continue
                opt = spec.options[long]
                if opt.takes_value:
                    rest = body[j + 1:]
                    take(opt, (rest or val) if (rest or eq) else None)
                    break
                take(opt, None)
        else:
            p.positionals.append(a)
        i += 1
    return p


# ---------------- 정책 문서 검사 ----------------
def inspect_policy_text(text: str) -> list[str]:
    """IAM/S3 정책 JSON의 위험 요소. 해석할 수 없으면 policy_unreadable."""
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return ["policy_unreadable"]
    stmts = doc.get("Statement") if isinstance(doc, dict) else None
    if isinstance(stmts, dict):
        stmts = [stmts]
    if not isinstance(stmts, list):
        return ["policy_unreadable"]
    flags: list[str] = []

    def as_list(v) -> list:
        return v if isinstance(v, list) else [v]

    for s in stmts:
        if not isinstance(s, dict):
            return ["policy_unreadable"]
        if any(k in s for k in ("NotPrincipal", "NotAction", "NotResource")):
            flags.append("negated_element")
        if s.get("Effect") == "Deny":
            continue
        principal = s.get("Principal")
        if principal is not None:
            # {"AWS": "*"}뿐 아니라 {"Service": "*"}, {"Federated": "*"} 등 어떤 키든 와일드카드면 위험하다
            vals = [x for v in principal.values() for x in as_list(v)] if isinstance(principal, dict) else as_list(principal)
            if "*" in vals:
                flags.append("wildcard_principal")
        for a in as_list(s.get("Action", [])):
            if isinstance(a, str) and (a == "*" or a.endswith(":*")):
                flags.append("wildcard_action")
    return list(dict.fromkeys(flags))


def resolve_in_workspace(raw: str, workspace: Path | None) -> Path | None:
    """로컬 경로가 세션 작업 폴더 안이면 해석된 경로, 아니면 None. 작업 폴더가 없으면 모두 거부한다."""
    if workspace is None:
        return None
    path = re.sub(r"^(?:file|fileb)://", "", raw)
    if not path or path == "-" or re.search(r"[\x00$%~]", path) or re.match(r"^[a-z][a-z0-9+.-]*://", path, re.I):
        return None
    ws = workspace.resolve()
    target = (ws / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    try:
        target.relative_to(ws)
    except ValueError:
        return None
    return target


# ---------------- 가드 ----------------
class CommandGuard:
    def __init__(
        self,
        index: CommandIndex,
        snapshot: CliSnapshot,
        *,
        workspace: Path | None = None,
        allowed_services: frozenset[str] | None = None,
    ):
        self.index = index
        self.snapshot = snapshot
        self.workspace = Path(workspace) if workspace else None
        self.allowed_services = frozenset(allowed_services) if allowed_services is not None else index.aws_services

    def check(self, command: str, masker: Masker | None = None) -> CommandVerdict:
        """어떤 예외도 차단으로 바꾼다(fail-closed). 앞 절차 결과 참조(`@{절차.필드}`, D-48)가 있으면 참조 자리를 리소스 ID 모양의 가짜 값으로
        바꿔 템플릿을 검사하고 판정의 `command`는 원문(템플릿) 그대로 둔다(`uses_output` 표시). 실행 전에는 치환한 명령을 다시 검사한다."""
        try:
            if isinstance(command, str) and "@{" in command:
                if has_unparsed_ref(command):
                    return _blocked(command, "bad_reference")
                if len(find_refs(command)) > 4:
                    return _blocked(command, "too_many_references")
                v = self._check(with_dummy(command), masker)
                return dataclasses.replace(v, command=command, flags=tuple(dict.fromkeys((*v.flags, "uses_output"))))
            return self._check(command, masker)
        except CliUnavailable:
            return _blocked(command, "cli_unavailable")
        except Exception:  # noqa: BLE001
            return _blocked(command, "guard_error")

    def _check(self, command: str, masker: Masker | None) -> CommandVerdict:
        if not isinstance(command, str) or not command.strip():
            return _blocked(command if isinstance(command, str) else "", "empty")
        masked_tokens, blocks = _split(command.strip())
        if blocks:
            return _blocked(command, *blocks)
        if masker is not None and (unresolved := masker.unresolved(command)):
            return _blocked(command, "unresolved_placeholder")
        # 위험 판단은 복원된 값으로 한다. 복원 결과는 이 함수 밖으로 내보내지 않는다.
        tokens = [masker.unmask(t) for t in masked_tokens] if masker else list(masked_tokens)

        cli = tokens[0]
        if cli not in ALLOWED_CLIS:
            return _blocked(command, "disallowed_cli")
        if len(tokens) < 2 or tokens[1].startswith("-"):
            return _blocked(command, "global_option_before_subcommand")

        blocks, flags, requires = [], [], []
        chunk: Chunk | None
        if cli == "aws":
            if len(tokens) < 3 or tokens[2].startswith("-"):
                return _blocked(command, "operation_required")
            service, name, rest = tokens[1], tokens[2], tokens[3:]
            chunk = self.index.aws(service, name)
            if chunk is None:
                if service not in self.allowed_services:
                    return _blocked(command, "service_not_allowed")
                if (service, name) in DENY_OPERATIONS:
                    return _blocked(command, f"denied_operation:{DENY_OPERATIONS[(service, name)]}")
            forbidden = AWS_FORBIDDEN_OPTS
        else:
            chunk = self.index.kubectl(tokens[1:])
            if chunk is not None:
                service, name = chunk.service, chunk.name
                rest = tokens[1 + len(name.split()):]
            else:
                service, name, rest = "kubectl", *self._resolve_kubectl(tokens)
                if name is None:
                    return _blocked(command, "unknown_command")
            forbidden = KUBECTL_FORBIDDEN_OPTS

        spec = self.snapshot.get(cli, service, name)
        if spec is None:
            return _blocked(command, "unknown_command")
        parsed = _parse_args(rest, spec, greedy=(cli == "aws" and service != "s3"))

        for u in parsed.unknown:
            # 도움말에 없는 전역 옵션(--kubeconfig 등)도 금지 목록에 있으면 정확한 사유로 남긴다
            blocks.append(f"forbidden_option:{u}" if u in forbidden else f"unknown_option:{u}")
        for opt, _ in parsed.opts:
            if opt in forbidden:
                blocks.append(f"forbidden_option:{opt}")

        if cli == "aws" and service != "s3" and parsed.positionals:
            blocks.append("unexpected_argument")     # aws API 명령은 위치 인자가 없다(실행하면 aws가 오류를 낸다)

        # ---- 문서 근거 ----
        if chunk is not None and chunk.kind == "restricted":
            return _blocked(command, "restricted_command")
        if chunk is not None:
            kind, risk, grounded = chunk.kind, chunk.risk, True
            impact = dict(chunk.impact)
        else:
            kind, risk, grounded, impact = "mutate", "high", False, {}
            flags += ["fallback", "unknown_impact"]
            requires.append("fallback_approval")

        # ---- 시크릿 ----
        scan_text = " ".join(tokens)
        for lit in LITERAL_PLACEHOLDERS:
            scan_text = scan_text.replace(lit, "")
        if find_secrets(scan_text):
            blocks.append("secret_in_command")
        for opt in SECRET_VALUE_OPTS:
            for v in parsed.values(opt):
                if v not in LITERAL_PLACEHOLDERS:
                    blocks.append("secret_in_command")

        # ---- 전체 개방, 관리형 관리자 정책 ----
        if OPEN_CIDR.search(scan_text):
            flags.append("open_cidr")
        if any(_is_wide_cidr(m.group(1), int(m.group(2))) for m in WIDE_CIDR.finditer(scan_text) if not OPEN_CIDR.search(m.group(0))):
            flags.append("wide_cidr")
        # 대화형 옵션은 문서에서 manual로 지정한 명령만 쓴다(서버가 실행하면 끝나지 않는다)
        if cli == "kubectl" and kind != "manual" and any(parsed.has(o) for o in INTERACTIVE_OPTS):
            blocks.append("interactive_option")
        if ADMIN_POLICY.search(scan_text):
            flags.append("admin_policy")

        local_paths: list[str] = []
        self._rules(cli, service, name, parsed, flags, blocks, local_paths)

        if masker is not None and masker.prod_targets(scan_text):
            flags.append("prod_target")
        if chunk is not None and kind != "read" and impact.get("iac"):
            flags.append("iac_drift")

        flags = list(dict.fromkeys(flags))
        blocks = list(dict.fromkeys(blocks))
        if blocks:
            return CommandVerdict(False, False, command, kind, "high", grounded, chunk.id if chunk else None, tuple(blocks), tuple(flags), (), impact)

        if any(f in _STRONG for f in flags):
            requires.append("strong_warning")
        if "open_cidr" in flags:
            requires.append("open_cidr_confirm")
        if "recursive_delete" in flags or "sync_delete" in flags:
            requires.append("dry_run_first")
        if "prod_target" in flags:
            requires.append("prod_warning")
        if local_paths or any(f in flags for f in ("policy_unreadable",)):
            requires.append("show_file_contents")
        if any(f in HIGH_FLAGS for f in flags):
            risk = "high"

        return CommandVerdict(
            allow=True,
            executable=(kind != "manual"),
            command=command,
            kind=kind,
            risk=risk,
            grounded=grounded,
            chunk_id=chunk.id if chunk else None,
            flags=tuple(flags),
            requires=tuple(dict.fromkeys(requires)),
            impact=impact,
            local_paths=tuple(local_paths),
        )

    # ---- kubectl 문서 밖 하위 명령 해석 ----
    def _resolve_kubectl(self, tokens: list[str]) -> tuple[str | None, list[str]]:
        first = tokens[1]
        if first not in self.snapshot.kubectl_builtins():   # 플러그인 이름이면 도움말을 읽는 것만으로 실행되므로 건드리지 않는다
            return None, []
        spec = self.snapshot.get("kubectl", "kubectl", first)
        if spec is None:
            return None, []
        if spec.subcommands:           # config, rollout, set 같은 그룹 명령은 하위 명령이 있어야 한다
            if len(tokens) < 3 or tokens[2] not in spec.subcommands:
                return None, []
            return f"{first} {tokens[2]}", tokens[3:]
        return first, tokens[2:]

    # ---- 규칙 ----
    def _rules(self, cli, service, name, p: _Parsed, flags: list[str], blocks: list[str], local_paths: list[str]) -> None:
        if cli == "aws":
            self._aws_rules(service, name, p, flags, blocks, local_paths)
        else:
            self._kubectl_rules(name, p, flags, blocks, local_paths)

        # 로컬 경로(file://, --filename, s3 로컬 경로)는 세션 작업 폴더 안만 허용한다.
        for raw in self._local_path_candidates(cli, service, name, p):
            target = resolve_in_workspace(raw, self.workspace)
            if target is None:
                blocks.append("local_path_outside_workspace")
            else:
                local_paths.append(str(target.relative_to(self.workspace.resolve())).replace("\\", "/"))

        # 정책 문서(인라인 또는 작업 폴더 파일)의 위험 요소
        for opt in POLICY_OPTS:
            for v in p.values(opt):
                text: str | None = v
                if re.match(r"^(?:file|fileb)://", v):
                    target = resolve_in_workspace(v, self.workspace)
                    text = None
                    if target is not None and target.is_file() and target.stat().st_size <= MAX_POLICY_BYTES:
                        text = target.read_text(encoding="utf-8", errors="replace")
                if text is None:
                    flags.append("policy_unreadable")
                else:
                    flags.extend(inspect_policy_text(text))

    @staticmethod
    def _local_path_candidates(cli, service, name, p: _Parsed) -> list[str]:
        out = [v for _, vals in p.opts for v in vals if re.match(r"^(?:file|fileb)://", v)]
        if cli == "aws" and service == "s3" and name in ("cp", "mv", "sync"):
            out += [x for x in p.positionals if not x.startswith("s3://")]
        if cli == "kubectl":
            for opt in PATH_OPTS_KUBECTL:
                out += [v for v in p.values(opt) if not re.match(r"^(?:file|fileb)://", v)]
        return out

    @staticmethod
    def _aws_rules(service, name, p: _Parsed, flags, blocks, local_paths) -> None:
        if service == "s3":
            if name == "rm" and p.has("--recursive"):
                flags.append("recursive_delete")
            if name == "rb" and p.has("--force"):
                flags.append("recursive_delete")
            if name == "sync" and p.has("--delete"):
                flags.append("sync_delete")
        if service == "s3api":
            if name == "put-public-access-block":
                cfg = " ".join(p.values("--public-access-block-configuration")).lower()
                if "false" in cfg:
                    flags.append("public_access_relax")
            if name == "put-bucket-acl" and (p.value("--acl") or "").lower() in ("public-read", "public-read-write", "authenticated-read"):
                flags.append("public_acl")
        if service == "ssm":
            if p.has("--with-decryption"):
                blocks.append("ssm_decryption")
            for opt in ("--name", "--names", "--path"):
                for v in p.values(opt):
                    if not v.startswith("/aws/service/"):
                        blocks.append("ssm_scope")
        if service == "ec2":
            if name in ("terminate-instances", "stop-instances", "reboot-instances") and len(p.values("--instance-ids")) > 1:
                flags.append("multiple_targets")
            if name == "run-instances" and (p.has("--user-data")):
                flags.append("user_data_script")
        if service == "autoscaling" and name == "terminate-instance-in-auto-scaling-group":
            if p.has("--instance-ids") or p.has("--auto-scaling-group-name"):
                flags.append("multiple_targets")

    @staticmethod
    def _kubectl_rules(name, p: _Parsed, flags, blocks, local_paths) -> None:
        for opt in BLOCKING_OPTS:
            if p.has(opt):
                blocks.append("blocking_option")
        if name in TIMEOUT_REQUIRED and not p.has("--timeout"):
            blocks.append("timeout_required")
        if name.split()[0] == "config" and (len(name.split()) > 1 and name.split()[1] in MUTATING_KUBECONFIG or p.positionals[:1] and p.positionals[0] in MUTATING_KUBECONFIG):
            blocks.append("kubeconfig_mutation")

        resources = [r.lower() for x in p.positionals[:1] for r in x.split(",")]
        if name == "get":
            out = (p.value("--output") or "")
            if any(re.match(r"^secrets?(?:\.|/|$)", r) for r in resources) and REVEALING_OUTPUT.match(out):
                blocks.append("secret_output")
            # 파일로 대상을 지정하면 어떤 종류인지 알 수 없다. 값을 보여주는 출력 형식과 함께 쓰면 확인할 수 없으므로 막는다.
            if (p.has("--filename") or p.has("--kustomize")) and REVEALING_OUTPUT.match(out):
                blocks.append("secret_output_unverifiable")
        if name == "create" and p.positionals[:1] == ["secret"]:
            blocks.append("secret_creation")
        if name == "delete":
            if p.has("--all-namespaces"):
                blocks.append("delete_all_namespaces")
            if p.has("--all"):
                flags.append("delete_all")
            if any(re.match(r"^(?:namespaces?|ns)(?:/|$)", r) for r in resources):
                flags.append("namespace_delete")
            if not (p.positionals or p.has("--filename") or p.has("--selector") or p.has("--all") or p.has("--kustomize")):
                blocks.append("target_missing")
        if name == "scale" and (p.value("--replicas") or "") == "0":
            flags.append("scale_to_zero")
