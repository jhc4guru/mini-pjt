"""knowledge/cli 문서 검증 (실제 aws / kubectl 도움말과 대조).

실행: .venv\\Scripts\\python.exe tests\\check_docs.py
- 실제 AWS 호출은 하지 않는다(help 출력만 사용). AWS_* 환경변수는 자식 프로세스에 전달하지 않는다.
"""
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent / "knowledge" / "cli"
KINDS = {"read", "mutate", "manual", "restricted"}
RISKS = {"low", "medium", "high"}
CLIS = {"aws", "kubectl"}
DISRUPTION = {"none", "possible", "likely"}
REVERSIBLE = {"full", "partial", "irreversible"}
IMPACT_KEYS = {"disruption", "replaces", "reversible", "preflight", "dry_run", "iac", "long_running"}
GUARDS = {
    "open-cidr", "wildcard-principal", "wildcard-action", "admin-policy", "recursive-delete",
    "sync-delete", "public-access", "output-secret", "input-secret", "local-file", "no-follow",
    "delete-all", "file-input",
}

ENV = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
ENV["AWS_PAGER"] = ""

errors: list[str] = []
warnings: list[str] = []


def run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=90, env=ENV)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, re.sub(r".\x08", "", out)
    except Exception as e:  # noqa: BLE001
        return 99, f"{type(e).__name__}: {e}"


def split_doc(path: Path):
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return None, None
    return yaml.safe_load(m.group(1)), m.group(2)


def has_opt(text: str, opt: str) -> bool:
    return re.search(re.escape(opt) + r"(?![A-Za-z0-9-])", text) is not None


def aws_required_in_help(text: str) -> set[str]:
    m = re.search(r"synopsis\s*\n\**\s*\n(.*?)\n(?:description|options)\s*\n", text, re.S | re.I)
    if not m:
        return set()
    req = set()
    for line in m.group(1).splitlines():
        s = line.strip()
        mm = re.match(r"^(--[a-z0-9-]+)\b", s)
        if mm:
            req.add(mm.group(1))
    return req


def check_command(job):
    cli, service, name, req, opt, rel = job
    if cli == "aws":
        code, text = run(["aws", service, *name.split(), "help"])
        if code != 0 or "invalid choice" in text.lower():
            return [f"{rel}: 실제 CLI에 없는 명령 `aws {service} {name}`"], []
        errs, warns = [], []
        for o in [*req, *opt]:
            if not has_opt(text, o):
                errs.append(f"{rel}: `aws {service} {name}`에 없는 옵션 {o}")
        help_req = aws_required_in_help(text)
        doc_req = set(req)
        if doc_req - help_req:
            warns.append(f"{rel}: `{name}` 문서는 필수라고 했지만 CLI에선 선택: {sorted(doc_req - help_req)}")
        if help_req - doc_req:
            warns.append(f"{rel}: `{name}` CLI 필수 옵션이 문서 required에 없음: {sorted(help_req - doc_req)}")
        return errs, warns
    code, text = run(["kubectl", *name.split(), "--help"])
    if code != 0:
        return [f"{rel}: 실제 CLI에 없는 명령 `kubectl {name}`"], []
    _, glob = KUBECTL_GLOBAL
    errs = []
    for o in [*req, *opt]:
        if not has_opt(text, o) and not has_opt(glob, o):
            errs.append(f"{rel}: `kubectl {name}`에 없는 옵션 {o}")
    return errs, []


KUBECTL_GLOBAL = run(["kubectl", "options"])


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    files = sorted(p for p in ROOT.rglob("*.md") if p.name != "SCHEMA.md")
    index: dict[tuple[str, str], dict[str, str]] = {}
    jobs = []
    docs = []
    seen: set[tuple[str, str, str]] = set()

    for path in files:
        rel = str(path.relative_to(ROOT.parent.parent))
        meta, body = split_doc(path)
        if meta is None:
            errors.append(f"{rel}: frontmatter 없음")
            continue
        cli, service = meta.get("cli"), meta.get("service")
        if cli not in CLIS:
            errors.append(f"{rel}: cli 값 오류 {cli!r}")
            continue
        if not service or not meta.get("topic") or not meta.get("commands"):
            errors.append(f"{rel}: service/topic/commands 필수")
            continue
        folder = path.relative_to(ROOT).parts[0]
        if folder != cli:
            errors.append(f"{rel}: 폴더({folder})와 cli({cli}) 불일치")
        headings = set(re.findall(r"^## (.+?)\s*$", body, re.M))
        for c in meta["commands"]:
            name = c.get("name")
            key = (cli, service, name)
            if key in seen:
                errors.append(f"{rel}: 명령 중복 {name}")
            seen.add(key)
            if c.get("kind") not in KINDS:
                errors.append(f"{rel}: {name} kind 오류 {c.get('kind')!r}")
            if c.get("risk") not in RISKS:
                errors.append(f"{rel}: {name} risk 오류 {c.get('risk')!r}")
            if c.get("kind") in ("manual", "restricted") and not c.get("reason"):
                errors.append(f"{rel}: {name} {c.get('kind')}는 reason 필수")
            for g in c.get("guard", []) or []:
                if g not in GUARDS:
                    errors.append(f"{rel}: {name} 알 수 없는 guard {g!r}")
            if c.get("kind") in ("mutate", "manual"):
                imp = c.get("impact")
                if not isinstance(imp, dict):
                    errors.append(f"{rel}: {name} impact 필수(mutate/manual)")
                else:
                    if imp.get("disruption") not in DISRUPTION:
                        errors.append(f"{rel}: {name} impact.disruption 오류 {imp.get('disruption')!r}")
                    if not isinstance(imp.get("replaces"), bool):
                        errors.append(f"{rel}: {name} impact.replaces는 true/false")
                    if imp.get("reversible") not in REVERSIBLE:
                        errors.append(f"{rel}: {name} impact.reversible 오류 {imp.get('reversible')!r}")
                    for k in imp:
                        if k not in IMPACT_KEYS:
                            errors.append(f"{rel}: {name} impact 알 수 없는 키 {k!r}")
            if name not in headings:
                errors.append(f"{rel}: 본문에 `## {name}` 제목 없음")
            index.setdefault((cli, service), {})[name] = c.get("kind")
            jobs.append((cli, service, name, c.get("required") or [], c.get("optional") or [], rel))
            docs.append((rel, cli, service, c))

    for rel, cli, service, c in docs:
        v = c.get("verify")
        if v:
            kind = index.get((cli, service), {}).get(v)
            if kind != "read":
                errors.append(f"{rel}: {c['name']} verify `{v}`는 같은 service의 read 명령이어야 함")
        elif c.get("kind") == "mutate":
            warnings.append(f"{rel}: mutate `{c['name']}`에 verify 없음")
        imp = c.get("impact") if isinstance(c.get("impact"), dict) else {}
        for pf in imp.get("preflight", []) or []:
            svc, _, pname = pf.rpartition(":")
            target = (cli, svc or service)
            if index.get(target, {}).get(pname) != "read":
                errors.append(f"{rel}: {c['name']} preflight `{pf}`는 같은 CLI의 read 명령이어야 함")

    with ThreadPoolExecutor(max_workers=6) as ex:
        for errs, warns in ex.map(check_command, jobs):
            errors.extend(errs)
            warnings.extend(warns)

    gl = ROOT / "glossary.yaml"
    if not gl.exists():
        errors.append("knowledge/cli/glossary.yaml 없음")
    else:
        g = yaml.safe_load(gl.read_text(encoding="utf-8")) or {}
        for sect in ("entities", "actions"):
            for t in g.get(sect, []) or []:
                if not t.get("canonical") or not t.get("aliases"):
                    errors.append(f"glossary.yaml: {sect} 항목에 canonical/aliases 필요: {t}")

    total = len(jobs)
    by_kind = {k: sum(1 for *_, c in docs if c.get("kind") == k) for k in KINDS}
    print(f"문서 {len(files)}개, 명령 {total}개 {by_kind}")
    for w in warnings:
        print("WARN ", w)
    for e in errors:
        print("ERROR", e)
    print(f"결과: 오류 {len(errors)}, 경고 {len(warnings)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
