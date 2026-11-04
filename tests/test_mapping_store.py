import sqlite3
from pathlib import Path

import pytest

from app.guards.mapping_store import MappingUnreadable, SqliteMappingStore
from app.guards.masking import Inventory, Masker

FIXTURE = Path(__file__).parent / "fixtures" / "inventory.sample.yaml"
SG = "sg-0123456789abcdef0"


def masker_with_values():
    m = Masker(Inventory.from_file(FIXTURE))
    masked = m.mask(f"aws ec2 describe-security-groups --group-ids {SG} i-0abc123def4567890")
    return m, masked


def test_restart_roundtrip_restores_values_and_numbering(tmp_path):
    db = tmp_path / "m.sqlite"
    m, masked = masker_with_values()
    store = SqliteMappingStore(db)
    store.save("s1", m)
    store.close()

    m2 = Masker(Inventory.from_file(FIXTURE))                  # 재시작 후 새 프로세스
    assert SqliteMappingStore(db).load_into("s1", m2)
    assert SG in m2.unmask(masked)
    assert m2.mask(SG) == m.mask(SG)                           # 같은 값은 같은 플레이스홀더
    new = m2.mask("sg-0fedcba9876543210")
    assert new not in masked and new.startswith("<SG_")        # 번호가 충돌하지 않는다


def test_db_file_has_no_plaintext_and_wrong_key_fails_closed(tmp_path):
    db = tmp_path / "m.sqlite"
    m, _ = masker_with_values()
    s = SqliteMappingStore(db)
    s.save("s1", m)
    s.close()
    raw = db.read_bytes()
    assert SG.encode() not in raw and b"i-0abc123def4567890" not in raw
    (tmp_path / "m.sqlite.key").write_bytes(b"\x01" * 32)       # 키가 바뀐 경우
    with pytest.raises(MappingUnreadable) as e:
        SqliteMappingStore(db).load_into("s1", Masker())
    assert SG not in str(e.value)


def test_ciphertext_is_bound_to_the_session(tmp_path):
    db = tmp_path / "m.sqlite"
    m, _ = masker_with_values()
    s = SqliteMappingStore(db)
    s.save("s1", m)
    con = sqlite3.connect(db)
    con.execute("UPDATE mapping_state SET session_id='s2' WHERE session_id='s1'")
    con.commit()
    con.close()
    with pytest.raises(MappingUnreadable):
        s.load_into("s2", Masker())


def test_ttl_expiry_and_delete(tmp_path):
    now = [1000.0]
    s = SqliteMappingStore(tmp_path / "m.sqlite", ttl_seconds=60, clock=lambda: now[0])
    m, _ = masker_with_values()
    s.save("a", m)
    s.save("b", m)
    now[0] += 61
    assert s.load_into("a", Masker()) is False                 # 만료되면 삭제하고 없음
    assert s.purge_expired() == 1                              # 남은 b
    s.save("c", m)
    s.delete("c")
    assert s.load_into("c", Masker()) is False


def test_malformed_state_is_rejected():
    with pytest.raises(ValueError):
        Masker().load_state({"fwd": {"x": "not-a-placeholder"}, "counters": {}})
