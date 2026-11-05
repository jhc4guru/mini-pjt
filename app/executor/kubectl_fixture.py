"""kubectl 고정 응답(mock). moto에는 쿠버네티스 API가 없어서 명령별로 미리 정한 응답을 돌려준다.

읽기 명령은 `MockWorld`의 상태(노드 버전, 격리된 노드 등)를 반영한 표를, 변경 명령은 kubectl이 내는 문구 형태의 응답을 돌려준다.
상태는 노드 격리(cordon/uncordon/drain)만 바뀐다(스케일, 롤아웃 등은 응답만 하고 상태를 바꾸지 않는다). 지원하지 않는 명령·리소스는
성공한 척하지 않고 실제 kubectl 같은 오류(종료 코드 1)로 돌려준다. 값은 모두 합성 값이다.
"""
from __future__ import annotations

import json

from app.executor.world import MockWorld

_RESOURCES = {"node": "nodes", "nodes": "nodes", "no": "nodes", "pod": "pods", "pods": "pods", "po": "pods",
              "deployment": "deployments", "deployments": "deployments", "deploy": "deployments",
              "namespace": "namespaces", "namespaces": "namespaces", "ns": "namespaces",
              "service": "services", "services": "services", "svc": "services",
              "poddisruptionbudget": "pdb", "poddisruptionbudgets": "pdb", "pdb": "pdb", "event": "events", "events": "events"}
_MUTATIONS = {"scale", "cordon", "uncordon", "drain", "label", "annotate", "patch", "taint", "apply", "create", "delete", "set image",
              "rollout restart", "rollout undo"}


def _parse(args: list[str]) -> tuple[list[str], dict[str, str]]:
    pos, opts, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and "=" in a:
            k, v = a.split("=", 1)
            opts[k] = v
        elif a in ("--all-namespaces", "-A"):
            opts["--all-namespaces"] = "true"
        elif a.startswith("-") and len(a) > 1 and not a[1:].isdigit():
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                opts[{"-n": "--namespace", "-o": "--output", "-l": "--selector", "-f": "--filename"}.get(a, a)] = args[i + 1]
                i += 1
            else:
                opts[a] = "true"
        else:
            pos.append(a)
        i += 1
    return pos, opts


def _table(rows: list[list[str]]) -> str:
    widths = [max(len(r[c]) for r in rows) + 3 for c in range(len(rows[0]))]
    return "\n".join("".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip() for r in rows) + "\n"


class KubectlFixture:
    def __init__(self, world: MockWorld):
        self.world = world

    def respond(self, tokens: list[str]) -> tuple[str, str, int]:
        """(stdout, stderr, 종료 코드)"""
        args = tokens[1:]
        verb = args[0] if args else ""
        two = " ".join(args[:2])
        if two in ("rollout status", "rollout history", "rollout restart", "rollout undo", "top node", "top pod", "auth can-i",
                   "config current-context", "set image"):
            verb, rest = two, args[2:]
        else:
            rest = args[1:]
        pos, opts = _parse(rest)
        try:
            return self._dispatch(verb, pos, opts)
        except _Unsupported as e:
            return "", f"error: {e}\n", 1

    # ---- 읽기 ----
    def _dispatch(self, verb: str, pos: list[str], opts: dict[str, str]) -> tuple[str, str, int]:
        w = self.world
        ns = opts.get("--namespace", "default")
        if verb == "version":
            return f"Client Version: v{w.control_plane_version}.2\nServer Version: v{w.control_plane_version}.4-eks-mock\n", "", 0
        if verb == "config current-context":
            return "mock-context\n", "", 0
        if verb == "auth can-i":
            return "yes\n", "", 0
        if verb in ("get", "describe"):
            return self._get(verb, pos, opts, ns)
        if verb == "top node":
            rows = [["NAME", "CPU(cores)", "CPU%", "MEMORY(bytes)", "MEMORY%"]] + [[n, "310m", "7%", "2105Mi", "28%"] for n in w.nodes]
            return _table(rows), "", 0
        if verb == "top pod":
            rows = [["NAME", "CPU(cores)", "MEMORY(bytes)"]] + [[p[1], "5m", "62Mi"] for p in w.pods if opts.get("--all-namespaces") or p[0] == ns]
            return _table(rows), "", 0
        if verb == "rollout status":
            return f'{pos[0] if pos else "deployment/web"} successfully rolled out\n', "", 0
        if verb == "rollout history":
            return f"{pos[0] if pos else 'deployment/web'}\nREVISION  CHANGE-CAUSE\n1         <none>\n2         <none>\n", "", 0
        if verb == "events":
            return "LAST SEEN   TYPE     REASON    OBJECT        MESSAGE\n12m         Normal   Started   pod/web-0     Started container web\n", "", 0
        if verb in _MUTATIONS:
            return self._mutate(verb, pos, opts, ns)
        raise _Unsupported(f'mock에서 지원하지 않는 kubectl 명령: {verb or "(없음)"}')

    def _get(self, verb: str, pos: list[str], opts: dict[str, str], ns: str) -> tuple[str, str, int]:
        w = self.world
        if not pos:
            raise _Unsupported("resource type을 지정해야 한다")
        res, _, name = pos[0].partition("/")
        if not name and len(pos) > 1:
            name = pos[1]
        kind = _RESOURCES.get(res.lower())
        if kind is None:
            raise _Unsupported(f'the server doesn\'t have a resource type "{res}"')
        out, wide, allns = opts.get("--output", ""), opts.get("--output") == "wide", bool(opts.get("--all-namespaces"))
        if verb == "describe":
            return f"Name:         {name or '(전체)'}\nNamespace:    {ns}\nStatus:       Running\nEvents:       <none>  (mock)\n", "", 0
        if kind == "nodes":
            names = [n for n in w.nodes if not name or n == name]
            if name and not names:
                return "", f'Error from server (NotFound): nodes "{name}" not found\n', 1
            if out == "json":
                return json.dumps({"items": [{"metadata": {"name": n}, "status": {"nodeInfo": {"kubeletVersion": w.node_full_version}}} for n in names]}, indent=2) + "\n", "", 0
            head = ["NAME", "STATUS", "ROLES", "AGE", "VERSION"] + (["INTERNAL-IP", "OS-IMAGE"] if wide else [])
            rows = [head] + [[n, w.node_status(n), "<none>", "42d", w.node_full_version] + ([w.node_ip(n), "Amazon Linux 2023"] if wide else []) for n in names]
            return _table(rows), "", 0
        if kind == "pods":
            pods = [p for p in w.pods if (allns or p[0] == ns) and (not name or p[1] == name)]
            if out == "json":
                return json.dumps({"items": [{"metadata": {"name": p[1], "namespace": p[0]}} for p in pods]}, indent=2) + "\n", "", 0
            head = (["NAMESPACE"] if allns else []) + ["NAME", "READY", "STATUS", "RESTARTS", "AGE"] + (["IP", "NODE"] if wide else [])
            rows = [head] + [([p[0]] if allns else []) + [p[1], p[2], "Running", str(p[3]), "12d"] + ([f"10.0.2.{20 + i}", w.nodes[i % len(w.nodes)]] if wide else [])
                             for i, p in enumerate(pods)]
            return (_table(rows) if pods else "No resources found.\n"), "", 0
        if kind == "deployments":
            deps = [d for d in w.deployments if (allns or d[0] == ns) and (not name or d[1] == name)]
            head = (["NAMESPACE"] if allns else []) + ["NAME", "READY", "UP-TO-DATE", "AVAILABLE", "AGE"]
            rows = [head] + [([d[0]] if allns else []) + [d[1], d[2], "2", "2", "30d"] for d in deps]
            return (_table(rows) if deps else "No resources found.\n"), "", 0
        if kind == "namespaces":
            return _table([["NAME", "STATUS", "AGE"]] + [[n, "Active", "90d"] for n in w.namespaces]), "", 0
        if kind == "pdb":
            return _table([["NAMESPACE", "NAME", "MIN AVAILABLE", "ALLOWED DISRUPTIONS"], ["payments", "web-pdb", "1", "1"]]), "", 0
        if kind == "services":
            return _table([["NAME", "TYPE", "CLUSTER-IP", "PORT(S)"], ["web", "ClusterIP", "172.20.5.10", "80/TCP"]]), "", 0
        return "LAST SEEN   TYPE     REASON    OBJECT        MESSAGE\n", "", 0

    # ---- 변경(응답만, 상태는 노드 격리만 바뀐다) ----
    def _mutate(self, verb: str, pos: list[str], opts: dict[str, str], ns: str) -> tuple[str, str, int]:
        w = self.world
        target = pos[0] if pos else ""
        res, _, name = target.partition("/")
        if verb in ("cordon", "uncordon", "drain", "taint"):
            node = target if verb != "taint" else (pos[1] if len(pos) > 1 else "")
            if node not in w.nodes:
                return "", f'Error from server (NotFound): nodes "{node}" not found\n', 1
            if verb == "cordon":
                w.cordoned.add(node)
                return f"node/{node} cordoned\n", "", 0
            if verb == "uncordon":
                w.cordoned.discard(node)
                return f"node/{node} uncordoned\n", "", 0
            if verb == "drain":
                w.cordoned.add(node)
                return f"node/{node} cordoned\nevicting pod {ns}/web-0\nnode/{node} drained\n", "", 0
            return f"node/{node} tainted\n", "", 0
        if verb == "apply":
            return "deployment.apps/web configured\n", "", 0
        if verb == "create":
            return f"{target or 'resource'} created\n", "", 0
        if verb == "delete":
            return f'{res or "resource"} "{name or (pos[1] if len(pos) > 1 else "")}" deleted\n', "", 0
        label = {"scale": "scaled", "rollout restart": "restarted", "rollout undo": "rolled back", "set image": "image updated",
                 "label": "labeled", "annotate": "annotated", "patch": "patched"}[verb]
        shown = target if "/" in target else (f"{res}/{pos[1]}" if len(pos) > 1 else target or "deployment/web")
        if "/" in shown and shown.split("/")[0] in ("deployment", "deploy"):
            shown = "deployment.apps/" + shown.split("/", 1)[1]
        return f"{shown} {label}\n", "", 0


class _Unsupported(Exception):
    pass
