"""설정 읽기. 모델 ID와 경로는 환경변수에서만 읽는다(.env 값은 출력하지 않는다)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge" / "cli"   # 직접 작성한 CLI 지식베이스(원본)
DATA_DIR = Path(os.environ.get("DEVOPS_DATA_DIR") or ROOT / "data")      # 실행 중 생성물(색인, DB, 트레이스). 지워도 재생성된다(컨테이너에서는 DEVOPS_DATA_DIR=/data)

_DEFAULT_EMBED_MODEL = "amazon.titan-embed-text-v2:0"


def load_env() -> None:
    """프로젝트 .env를 읽는다. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    load_dotenv(ROOT / ".env", override=False)


def inventory_path() -> tuple[Path, bool]:
    """(경로, 환경변수로 명시했는가). 인벤토리는 환경 고유 값(리소스 이름)이라 버전 관리하지 않는다."""
    explicit = os.environ.get("INVENTORY_FILE")
    return (Path(explicit), True) if explicit else (DATA_DIR / "inventory.yaml", False)


def region() -> str:
    return os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def embed_model_id() -> str:
    return os.environ.get("EMBED_MODEL", _DEFAULT_EMBED_MODEL)


def planner_mode() -> str:
    """계획 조사 방식(D-69): `loop`(기존: 모델이 도구를 하나씩 호출) | `batch`(일괄 조사 + 단계별 예산). 기본은 `batch`(D-73, 평가셋 채점 뒤 전환)이고 `loop`는 비교·안전망으로 남겨 둔다. 가드를 켜고 끄는 값이 아니다(두 방식 모두 가드·한도가 같다)."""
    mode = (os.environ.get("PLANNER_MODE") or "batch").strip().lower()
    if mode not in ("loop", "batch"):
        raise RuntimeError("환경변수 PLANNER_MODE는 loop 또는 batch여야 합니다.")
    return mode


def model_id(role: str) -> str:
    """role: MAIN | SUB | READ"""
    key = f"{role.upper()}_MODEL"
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f"환경변수 {key}가 설정되어 있지 않습니다(.env 확인).")
    return value
