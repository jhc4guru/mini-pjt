"""moto가 지원하지 않는 AWS 명령의 시뮬레이터(mock).

moto 서버는 EKS 업그레이드(`update-cluster-version` 등)와 ASG 인스턴스 리프레시를 지원하지 않는다(2026-11-04 확인). 이 명령들이
EKS 업그레이드 시나리오의 핵심 변경이라 상태를 가진 시뮬레이터로 대신한다. 응답 JSON은 실제 API의 모양을 따르고, 진행은 시간이 아니라
조회 횟수로 센다(`describe-update`, `describe-cluster`, `describe-instance-refreshes`를 `STEPS`번 부르면 완료). 그래서 poll step이
결정적으로 끝난다. 시뮬레이터가 바꾼 상태는 `MockWorld`(컨트롤 플레인·노드 버전)에 반영되어 kubectl 응답에도 보인다.

moto가 처리하는 `eks describe-cluster`는 moto의 응답을 받아 버전·상태만 시뮬레이션 상태로 덮어쓴다(`decorate`).
"""
from __future__ import annotations

import json
import time
import uuid

from app.executor.world import MockWorld

STEPS = 3                      # 조회 STEPS번째에 완료로 바뀐다
_ADDONS = ("coredns", "kube-proxy", "vpc-cni")


def _opts(tokens: list[str]) -> dict[str, str]:
    out, i = {}, 3
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--"):
            k, eq, v = t.partition("=")
            if eq:
                out[k] = v
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                out[k] = tokens[i + 1]
                i += 1
            else:
                out[k] = ""
        i += 1
    return out


def _dump(obj: dict) -> str:
    return json.dumps(obj, indent=4, ensure_ascii=False) + "\n"


class _Op:
    def __init__(self, kind: str, target: str):
        self.id, self.kind, self.target, self.polls = str(uuid.uuid4()), kind, target, 0

    def poll(self) -> int:
        self.polls += 1
        return self.polls

    @property
    def done(self) -> bool:
        return self.polls >= STEPS


class AwsSimulator:
    def __init__(self, world: MockWorld):
        self.world = world
        self._eks: dict[str, _Op] = {}                      # 클러스터 이름 → 진행 중인 버전 업데이트
        self._addon: dict[tuple[str, str], _Op] = {}
        self._refresh: dict[str, _Op] = {}                  # ASG 이름 → 인스턴스 리프레시

    # ---- 처리 여부 ----
    def handle(self, tokens: list[str]) -> str | None:
        """시뮬레이터가 맡는 명령이면 응답 문자열, 아니면 None(moto로 보낸다)."""
        if len(tokens) < 3 or tokens[0] != "aws":
            return None
        key, o = (tokens[1], tokens[2]), _opts(tokens)
        fn = {
            ("eks", "update-cluster-version"): self._eks_update, ("eks", "describe-update"): self._eks_describe_update,
            ("eks", "list-updates"): self._eks_list_updates, ("eks", "list-addons"): self._list_addons,
            ("eks", "describe-addon"): self._describe_addon, ("eks", "update-addon"): self._update_addon,
            ("eks", "describe-addon-versions"): self._addon_versions, ("eks", "list-insights"): self._insights,
            ("autoscaling", "start-instance-refresh"): self._refresh_start,
            ("autoscaling", "describe-instance-refreshes"): self._refresh_describe,
            ("autoscaling", "cancel-instance-refresh"): self._refresh_cancel,
            ("ssm", "get-parameter"): self._ssm_get,
        }.get(key)
        return fn(o) if fn else None

    def decorate(self, tokens: list[str], stdout: str) -> str:
        """moto가 처리한 `eks describe-cluster` 응답의 버전·상태를 시뮬레이션 상태로 덮어쓴다."""
        if tokens[1:3] != ["eks", "describe-cluster"]:
            return stdout
        try:
            data = json.loads(stdout)
            c = data["cluster"]
        except (ValueError, KeyError, TypeError):
            return stdout
        op = self._eks.get(_opts(tokens).get("--name", ""))
        if op is not None:
            self._advance_eks(op)
            c["status"] = "ACTIVE" if op.done else "UPDATING"
        c["version"] = self.world.control_plane_version
        return _dump(data)

    # ---- EKS 컨트롤 플레인 ----
    def _update_obj(self, op: _Op, params: list[dict]) -> dict:
        status = "Successful" if op.done else ("InProgress" if op.polls else "InProgress")
        return {"id": op.id, "status": status, "type": "VersionUpdate" if op.kind == "version" else "AddonUpdate", "params": params,
                "createdAt": time.time(), "errors": []}

    def _advance_eks(self, op: _Op) -> None:
        was = op.done
        op.poll()
        if op.done and not was:
            self.world.control_plane_version = op.target

    def _eks_update(self, o: dict) -> str:
        name, target = o.get("--name", ""), o.get("--kubernetes-version", "")
        op = _Op("version", target)
        self._eks[name] = op
        return _dump({"update": self._update_obj(op, [{"type": "Version", "value": target}, {"type": "PlatformVersion", "value": "eks.1"}])})

    def _eks_describe_update(self, o: dict) -> str:
        op = self._eks.get(o.get("--name", ""))
        if op is None or o.get("--update-id") not in (op.id, None):
            return _dump({"error": "ResourceNotFoundException (mock)"})
        self._advance_eks(op)
        return _dump({"update": self._update_obj(op, [{"type": "Version", "value": op.target}])})

    def _eks_list_updates(self, o: dict) -> str:
        op = self._eks.get(o.get("--name", ""))
        return _dump({"updateIds": [op.id] if op else []})

    # ---- EKS 애드온·인사이트(정적) ----
    def _list_addons(self, o: dict) -> str:
        return _dump({"addons": list(_ADDONS)})

    def _describe_addon(self, o: dict) -> str:
        name = o.get("--addon-name", "")
        op = self._addon.get((o.get("--cluster-name", ""), name))
        if op is not None:
            op.poll()
        version = op.target if op is not None and op.done else "v1.11.4-eksbuild.1"
        return _dump({"addon": {"addonName": name, "clusterName": o.get("--cluster-name", ""), "status": "UPDATING" if op is not None and not op.done else "ACTIVE",
                                "addonVersion": version}})

    def _update_addon(self, o: dict) -> str:
        op = _Op("addon", o.get("--addon-version", "v1.11.4-eksbuild.2"))
        self._addon[(o.get("--cluster-name", ""), o.get("--addon-name", ""))] = op
        return _dump({"update": self._update_obj(op, [{"type": "AddonVersion", "value": op.target}])})

    def _addon_versions(self, o: dict) -> str:
        name = o.get("--addon-name") or "coredns"
        k8s = o.get("--kubernetes-version", self.world.control_plane_version)
        return _dump({"addons": [{"addonName": name, "type": "networking", "addonVersions": [
            {"addonVersion": "v1.11.4-eksbuild.2", "compatibilities": [{"clusterVersion": k8s, "defaultVersion": True}]},
            {"addonVersion": "v1.11.4-eksbuild.1", "compatibilities": [{"clusterVersion": k8s, "defaultVersion": False}]}]}]})

    def _insights(self, o: dict) -> str:
        return _dump({"insights": [{"id": "insight-mock-1", "name": "Deprecated API usage", "category": "UPGRADE_READINESS",
                                    "insightStatus": {"status": "PASSING", "reason": "No deprecated API usage detected (mock)"}}]})

    # ---- ASG 인스턴스 리프레시 ----
    def _refresh_start(self, o: dict) -> str:
        op = _Op("refresh", self.world.control_plane_version)
        self._refresh[o.get("--auto-scaling-group-name", "")] = op
        return _dump({"InstanceRefreshId": op.id})

    def _refresh_describe(self, o: dict) -> str:
        asg = o.get("--auto-scaling-group-name", "")
        op = self._refresh.get(asg)
        if op is None:
            return _dump({"InstanceRefreshes": []})
        was = op.done
        op.poll()
        if op.done and not was:
            self.world.node_version = self.world.control_plane_version     # 새 노드가 컨트롤 플레인 버전으로 교체된다
        if getattr(op, "cancelled", False):
            status, pct = "Cancelled", 0
        elif op.done:
            status, pct = "Successful", 100
        else:
            status, pct = ("Pending", 0) if op.polls == 1 else ("InProgress", 50)
        return _dump({"InstanceRefreshes": [{"InstanceRefreshId": op.id, "AutoScalingGroupName": asg, "Status": status,
                                              "PercentageComplete": pct, "InstancesToUpdate": 0 if op.done else len(self.world.nodes)}]})

    def _refresh_cancel(self, o: dict) -> str:
        op = self._refresh.get(o.get("--auto-scaling-group-name", ""))
        if op is None:
            return _dump({"error": "ActiveInstanceRefreshNotFound (mock)"})
        op.cancelled = True            # type: ignore[attr-defined]
        return _dump({"InstanceRefreshId": op.id})

    # ---- SSM ----
    def _ssm_get(self, o: dict) -> str | None:
        name = o.get("--name", "")
        if name.startswith("/aws/service/eks/optimized-ami/"):
            value = "ami-0123456789abcdef0"
        elif name.startswith(("/aws/service/canonical/ubuntu/", "/aws/service/ami-amazon-linux-latest/")):
            value = "ami-0a1b2c3d4e5f60718"                  # Ubuntu·Amazon Linux 공개 파라미터(합성 AMI ID). 모델이 AMI를 찾을 수 있게 한다
        else:
            return None                                     # 그 밖의 파라미터는 moto가 처리한다
        return _dump({"Parameter": {"Name": name, "Type": "String", "Value": value, "Version": 1}})
