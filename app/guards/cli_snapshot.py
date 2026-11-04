"""실제 CLI 도움말에서 만든 옵션 스냅샷.

문서(knowledge/cli)의 optional은 전체 목록이 아니므로, 출력 가드는 이 스냅샷으로 "그 명령에 실제로 있는 옵션인지"를 검사한다.
- `aws <서비스> <명령> help`, `kubectl <명령> --help`를 읽기만 한다. AWS API는 호출하지 않으며 AWS_* 환경변수는 자식 프로세스에 전달하지 않는다.
- 결과는 data/cli_snapshot.json에 캐시한다(CLI 버전이 바뀌면 버린다). 문서에 없는 명령(폴백)도 처음 쓸 때 만들어 캐시한다.
- 명령이 실제로 존재하는지도 여기서 판정한다(없으면 get()이 None).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from app.config import DATA_DIR

Runner = Callable[[list[str]], tuple[int, str]]

# kubectl의 전역 옵션 중 쓸 수 있는 것(나머지는 명령 가드가 금지한다)
_KUBECTL_GLOBAL_ALLOWED = {"--namespace": ("-n", True), "--request-timeout": (None, True)}


class CliUnavailable(RuntimeError):
    """aws/kubectl을 실행할 수 없다(설치 안 됨, 시간 초과)."""


@dataclass(frozen=True)
class OptionSpec:
    long: str
    short: str | None
    takes_value: bool


@dataclass(frozen=True)
class CommandSpec:
    options: dict[str, OptionSpec] = field(default_factory=dict)
    subcommands: tuple[str, ...] = ()           # kubectl 그룹 명령(config, rollout 등)의 하위 명령

    @property
    def shorts(self) -> dict[str, str]:
        return {o.short: o.long for o in self.options.values() if o.short}


def default_runner(cmd: list[str]) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env["AWS_PAGER"] = ""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=env)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise CliUnavailable(f"{cmd[0]} 실행 불가: {type(e).__name__}") from None
    return p.returncode, re.sub(r".\x08", "", (p.stdout or "") + (p.stderr or ""))


# ---------------- 파서 ----------------
_OPT = re.compile(r"--[a-z0-9][a-z0-9-]*")


def parse_aws_help(text: str) -> CommandSpec | None:
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip().lower() == "synopsis"), None)
    if start is None:
        return None
    options: dict[str, OptionSpec] = {}
    for i in range(start + 2, len(lines)):
        line = lines[i]
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if line.strip() and not line.startswith(" ") and nxt and set(nxt) == {"*"}:
            break  # 다음 구역 제목
        for m in _OPT.finditer(line):
            tail = line[m.end():]
            # "--opt <value>" 이면 값을 받는다. 같은 줄의 다음 옵션 앞까지만 본다.
            nxt_opt = _OPT.search(tail)
            seg = tail[: nxt_opt.start()] if nxt_opt else tail
            options[m.group(0)] = OptionSpec(m.group(0), None, "<" in seg)
    return CommandSpec(options=options)


_K_OPT = re.compile(r"^\s{2,}(?:(-[A-Za-z]),\s*)?(--[A-Za-z0-9-]+)(?:=(.*))?:\s*$")


def parse_kubectl_help(text: str) -> CommandSpec:
    options: dict[str, OptionSpec] = {}
    subs: list[str] = []
    section = ""
    for line in text.splitlines():
        s = line.strip()
        if s in ("Options:", "Available Commands:"):
            section = s
            continue
        if not s:
            if section == "Available Commands:":
                section = ""
            continue
        if s.startswith("Usage:"):
            section = ""
        if section == "Options:":
            m = _K_OPT.match(line)
            if m:
                default = m.group(3)
                options[m.group(2)] = OptionSpec(m.group(2), m.group(1), default is not None and default not in ("false", "true"))
        elif section == "Available Commands:":
            m = re.match(r"^\s{2}([a-z][a-z0-9-]*)\s{2,}", line)
            if m:
                subs.append(m.group(1))
    return CommandSpec(options=options, subcommands=tuple(subs))


def _with_kubectl_globals(spec: CommandSpec) -> CommandSpec:
    opts = dict(spec.options)
    for long, (short, tv) in _KUBECTL_GLOBAL_ALLOWED.items():
        opts.setdefault(long, OptionSpec(long, short, tv))
    return CommandSpec(options=opts, subcommands=spec.subcommands)


# ---------------- 스냅샷 ----------------
def _key(cli: str, service: str, name: str) -> str:
    return f"{cli}:{service}:{name}"


class CliSnapshot:
    def __init__(self, path: Path | None = None, runner: Runner | None = None, persist: bool = True):
        self.path = Path(path or DATA_DIR / "cli_snapshot.json")
        self._run = runner or default_runner
        self._persist = persist
        self._lock = threading.Lock()
        self._specs: dict[str, CommandSpec] = {}
        self._missing: set[str] = set()      # 존재하지 않는 명령(메모리에만)
        self._builtins: frozenset[str] | None = None
        self._versions: dict[str, str] | None = None
        self._load()

    # ---- 저장/불러오기 ----
    def _cli_versions(self) -> dict[str, str]:
        if self._versions is None:
            v = {}
            for cli, cmd in (("aws", ["aws", "--version"]), ("kubectl", ["kubectl", "version", "--client"])):
                try:
                    code, out = self._run(cmd)
                    v[cli] = out.strip().splitlines()[0] if code == 0 and out.strip() else "unknown"
                except CliUnavailable:
                    v[cli] = "unavailable"
            self._versions = v
        return self._versions

    def _load(self) -> None:
        if not (self._persist and self.path.exists()):
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("versions") != self._cli_versions():
                return  # CLI 버전이 바뀌었으면 버리고 다시 만든다
            for k, v in data.get("specs", {}).items():
                self._specs[k] = CommandSpec(
                    options={o["long"]: OptionSpec(o["long"], o.get("short"), o["takes_value"]) for o in v["options"]},
                    subcommands=tuple(v.get("subcommands", ())),
                )
        except (OSError, ValueError, KeyError):
            self._specs.clear()

    def save(self) -> None:
        if not self._persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "versions": self._cli_versions(),
            "specs": {
                k: {
                    "options": [{"long": o.long, "short": o.short, "takes_value": o.takes_value} for o in s.options.values()],
                    "subcommands": list(s.subcommands),
                }
                for k, s in sorted(self._specs.items())
            },
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def kubectl_builtins(self) -> frozenset[str]:
        """kubectl 내장 명령 이름. 플러그인(PATH의 kubectl-xxx)이 제공하는 명령은 제외한다.

        문서에 없는 kubectl 명령의 도움말(`kubectl <이름> --help`)을 읽을 때, 이름이 플러그인이면 그 실행 파일이 실행되므로
        내장 명령인 것만 도움말을 읽는다.
        """
        with self._lock:
            if self._builtins is not None:
                return self._builtins
        code, text = self._run(["kubectl", "--help"])
        if code != 0:
            raise CliUnavailable("kubectl --help 실패")
        names, skip = set(), False
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("Usage:"):
                break
            if s.endswith(":") and not line.startswith(" "):
                skip = s.lower().startswith("subcommands provided by plugins")
                continue
            m = re.match(r"^\s{2}([a-z][a-z0-9-]*)\s{2,}\S", line)
            if m and not skip:
                names.add(m.group(1))
        with self._lock:
            self._builtins = frozenset(names)
            return self._builtins

    # ---- 조회 ----
    def get(self, cli: str, service: str, name: str) -> CommandSpec | None:
        """명령의 옵션 명세. 실제로 없는 명령이면 None. aws/kubectl을 못 쓰면 CliUnavailable."""
        key = _key(cli, service, name)
        with self._lock:
            if key in self._specs:
                return self._specs[key]
            if key in self._missing:
                return None
        spec = self._fetch(cli, service, name)
        with self._lock:
            if spec is None:
                self._missing.add(key)
            else:
                self._specs[key] = spec
                self.save()
        return spec

    def _fetch(self, cli: str, service: str, name: str) -> CommandSpec | None:
        if cli == "aws":
            code, text = self._run(["aws", service, *name.split(), "help"])
            if code != 0 or "invalid choice" in text.lower():
                return None
            return parse_aws_help(text)
        if cli == "kubectl":
            code, text = self._run(["kubectl", *name.split(), "--help"])
            if code != 0:
                return None
            return _with_kubectl_globals(parse_kubectl_help(text))
        raise ValueError(f"지원하지 않는 CLI: {cli}")

    def build(self, keys: Iterable[tuple[str, str, str]], workers: int = 6, only_missing: bool = False) -> dict[str, int]:
        """문서에 있는 명령들의 스냅샷을 미리 만든다. only_missing이면 이미 캐시된 것은 다시 읽지 않는다."""
        keys = list(dict.fromkeys(keys))
        if only_missing:
            keys = [k for k in keys if _key(*k) not in self._specs]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            specs = list(ex.map(lambda k: self._fetch(*k), keys))
        with self._lock:
            ok = 0
            for k, s in zip(keys, specs):
                if s is None:
                    self._missing.add(_key(*k))
                else:
                    self._specs[_key(*k)] = s
                    ok += 1
            self.save()
        return {"built": ok, "missing": len(keys) - ok}
