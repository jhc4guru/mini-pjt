"""설치별 무작위 키 파일. DB 밖에 둔다(DB 파일만 유출돼도 키가 없으면 해시·암호문을 풀 수 없게)."""
from __future__ import annotations

import secrets
from pathlib import Path


def key_path_for(db_path: str, default_name: str) -> Path:
    """기본 DB 이름(`audit.sqlite` ↔ `audit.key`)이면 같은 폴더의 `default_name`, 명시한 경로면 `<db>.key`."""
    p = Path(db_path)
    if p.name == default_name.replace(".key", ".sqlite"):
        return p.parent / default_name
    return p.with_name(p.name + ".key")


def load_or_create_key(path: Path, size: int = 32) -> bytes:
    if path.exists():
        k = path.read_bytes()
        if len(k) >= size:
            return k
    path.parent.mkdir(parents=True, exist_ok=True)
    k = secrets.token_bytes(size)
    path.write_bytes(k)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return k
