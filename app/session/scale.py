"""큰 작업 요청 감지(D-59): 한 번에 계획하면 모델 호출이 많아 시간이 오래 걸리고 사용량 한도에 닿을 수 있는 요청에 "나눠서 요청" 안내를 보이기 위한 규칙.

모델이 아니라 **낱말 규칙**이다(결정적이고 토큰이 들지 않는다). 안내만 하고 요청은 막지 않는다. 틀리게 판정해도(큰 요청을 놓치거나 작은 요청을 크다고 봄)
계획 생성에는 영향이 없다. 마스킹된 요청 문장만 받는다.
"""
from __future__ import annotations

import re

# 큰 작업의 대표 유형: 클러스터·노드 등의 버전 업그레이드, 마이그레이션
_UPGRADE = re.compile(r"업그레이드|upgrade", re.I)
_UPGRADE_TARGET = re.compile(r"클러스터|노드|컨트롤\s*플레인|(?<![a-z])(?:cluster|eks|node|asg|control\s*plane)(?![a-z])", re.I)      # 영문은 다른 단어 안(weeks 등)에서 맞지 않게 경계를 둔다
_MIGRATION = re.compile(r"마이그레이션|migrat|이관", re.I)
# 이미 단계로 나눠 요청한 문장은 다시 안내하지 않는다
_ALREADY_SPLIT = re.compile(r"사전\s*작업|사후\s*작업|사전\s*점검만|사전만|사후만", re.I)

SPLIT_NOTICE = (
    "큰 작업으로 보입니다. 한 번에 계획하면 시간이 오래 걸리고 모델 사용량 한도에 닿아 중단될 수 있습니다. "
    "위험한 변경 직전을 경계로 나눠서(예: 사전 작업 / 사후 작업) plan을 따로 요청하는 것을 권합니다. 이 요청은 그대로 진행합니다."
)


def looks_large(masked_text: str) -> bool:
    """마스킹된 요청이 한 번에 계획하기엔 큰 작업(클러스터 업그레이드·마이그레이션)으로 보이면 True. 이미 사전/사후로 나눈 요청은 False."""
    t = masked_text or ""
    if _ALREADY_SPLIT.search(t):
        return False
    return bool((_UPGRADE.search(t) and _UPGRADE_TARGET.search(t)) or _MIGRATION.search(t))
