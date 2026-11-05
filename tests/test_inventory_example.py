"""인벤토리 예시 파일(`inventory.example.yaml`)이 실제로 읽히고, 실제 환경 값이 들어 있지 않으며, 가이드 주석이 있는지 확인한다."""
from pathlib import Path

from app.guards.masking import Inventory, Masker
from app.guards.patterns import find_secrets, find_sensitive

EXAMPLE = Path(__file__).resolve().parent.parent / "inventory.example.yaml"


def test_the_example_loads_and_masks_its_names():
    inv = Inventory.from_file(EXAMPLE)
    assert set(inv.names) >= {"CLUSTER", "ASG"} and all(inv.names[c] for c in ("CLUSTER", "ASG"))
    m = Masker(inv)
    text = f"{inv.names['CLUSTER'][0]} 클러스터의 {inv.names['ASG'][0]} ASG"
    assert "<CLUSTER_1>" in m.mask(text) and "<ASG_1>" in m.mask(text)


def test_the_example_has_a_commented_guide_and_no_sensitive_values():
    text = EXAMPLE.read_text(encoding="utf-8")
    for phrase in ("마스킹", "lookup_inventory", "prod", "data/inventory.yaml", "INVENTORY_FILE", "허용되는", "CLUSTER", "NODEGROUP", "ASG", "BUCKET", "NAMESPACE"):
        assert phrase in text, phrase
    assert text.count("#") > 40                                                         # 설명 주석이 충분히 달려 있다
    assert not find_sensitive(text) and not find_secrets(text)
