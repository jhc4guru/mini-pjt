"""mock 환경의 상태(시뮬레이터와 kubectl 고정 응답이 공유한다). 실제 환경의 값이 아니다(합성 값)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MockWorld:
    control_plane_version: str = "1.35"
    node_version: str = "1.35"
    nodes: list[str] = field(default_factory=lambda: ["worker-a1", "worker-a2", "worker-b1"])
    cordoned: set[str] = field(default_factory=set)
    namespaces: tuple[str, ...] = ("default", "kube-system", "payments")
    # (namespace, 이름, 준비 상태, 재시작 횟수)
    pods: tuple[tuple[str, str, str, int], ...] = (
        ("payments", "web-0", "1/1", 0), ("payments", "web-1", "1/1", 0),
        ("kube-system", "coredns-5d78c9869d-abcde", "1/1", 0), ("kube-system", "kube-proxy-x7k2p", "1/1", 0),
        ("kube-system", "aws-node-m4q8z", "2/2", 0),
    )
    deployments: tuple[tuple[str, str, str], ...] = (("payments", "web", "2/2"), ("kube-system", "coredns", "2/2"))

    def node_ip(self, name: str) -> str:
        return f"10.0.1.{10 + self.nodes.index(name)}"

    def node_status(self, name: str) -> str:
        return "Ready,SchedulingDisabled" if name in self.cordoned else "Ready"

    @property
    def node_full_version(self) -> str:
        return f"v{self.node_version}.2"
