"""가드레일이 공유하는 패턴: 민감값, 시크릿, 프롬프트 인젝션.

LLM·임베딩 같은 외부 모델 경계로 나가는 텍스트에는 민감값이 없어야 한다.
리소스 이름(버킷, 클러스터 등)은 정규식으로 못 찾으므로 인벤토리 기반 마스킹(masking.py)이 따로 맡는다.
패턴은 값을 돌려주지만, 호출한 쪽은 값을 로그·오류에 그대로 내지 않는다(종류만 낸다).
"""
from __future__ import annotations

import re

# 단어 경계는 ASCII 기준으로 본다. 파이썬의 `\b`·`\w`는 한글을 단어 문자로 취급해서, 값 뒤에 조사가 붙으면(`sg-0123…를`, `123456789012의`)
# 경계가 사라져 마스킹·시크릿·인젝션 패턴을 피해 간다. 한국어 요청에서는 값 뒤에 조사가 늘 붙으므로 모든 패턴이 이 경계를 쓴다.
# 패턴 문자열 안의 `\b`는 `_c()`가 ASCII 경계(_B)로 바꿔 컴파일한다.
_W = "A-Za-z0-9_"
_B = rf"(?:(?<=[{_W}])(?![{_W}])|(?<![{_W}])(?=[{_W}]))"


def _c(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern.replace(r"\b", _B), flags)


# 공개 상수는 민감값이 아니다. 전체 개방(0.0.0.0/0) 판정에 실제 값이 필요하다.
_PUBLIC_CIDRS = r"(?!0\.0\.0\.0/0(?![0-9]))"

# 공개된 AMI 제작사 계정 ID(`describe-images --owners`에 쓰는 값)는 비밀이 아니라 마스킹·누출 검사에서 제외한다(D-45).
# 정확히 이 12자리와 일치할 때만 제외하고, 이 목록에 없는 12자리 숫자는 계속 계정 ID로 본다.
PUBLIC_ACCOUNT_IDS = {
    "099720109477": "Canonical(Ubuntu)", "137112412989": "Amazon(Amazon Linux)", "309956199498": "Red Hat", "136693071363": "Debian",
    "013907871322": "SUSE", "679593333241": "AWS Marketplace",
}
_PUBLIC_ACCOUNT_IDS = "|".join(PUBLIC_ACCOUNT_IDS)

# 마스킹 대상. ARN은 계정 자리가 숫자이거나 비어 있는 것만 잡는다(ASCII 문자까지만 ARN으로 보고, 뒤에 붙은 조사는 남긴다).
# 계정이 `aws`인 AWS 관리형 정책 ARN(arn:aws:iam::aws:policy/...)은 공개 값이라 마스킹하지 않는다.
SENSITIVE_PATTERNS: dict[str, re.Pattern[str]] = {
    "account_id": re.compile(rf"(?<![{_W}.])(?!(?:{_PUBLIC_ACCOUNT_IDS})(?![{_W}.]))\d{{12}}(?![{_W}.])"),
    "arn": _c(r"\barn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d*:[^\s\"',;)\u0080-\U0010ffff]+"),
    # AWS 리소스 ID는 접두사가 매우 많다(vpc-cidr-assoc-, rtbassoc-, vpce-, key- ...). 목록으로 잡으면 새 접두사가 마스킹을 피해 가므로,
    # 소문자 접두사 1~3토막 + 16진수 8~17자리 꼴이면 모두 리소스 ID로 본다(과하게 잡는 쪽이 안전하다).
    "resource_id": _c(r"\b(?:[a-z]{1,12}-){1,3}[0-9a-f]{8,17}\b"),
    "access_key": _c(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "ip_or_cidr": _c(_PUBLIC_CIDRS + r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
}

# 시크릿: 마스킹하지 않고 입력 자체를 막는다(시스템에 들어오면 안 되는 값).
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "secret_assignment": _c(
        r"(?i)\b(?:password|passwd|pwd|secret(?:[_ -]?access)?[_ -]?key|api[_-]?key|token|private[_-]?key)\b\s*[:=]\s*[^\s,;]+"
    ),
    "ko_secret_assignment": re.compile(r"(?:비밀번호|패스워드|시크릿\s*키|토큰)\s*[:=]\s*[^\s,;]+"),
    # `:`·`=` 없이 문장으로 쓴 시크릿("비밀번호는 Passw0rd!x 입니다"). 값은 ASCII 6자 이상이고 숫자나 기호를 포함해야 한다(일반 낱말 오탐 방지)
    "ko_secret_phrase": re.compile(
        r"(?:비밀번호|패스워드|암호|시크릿\s*키|토큰)\s*(?:은|는|이|가|을|를)?\s*[:=]?\s*"
        r"(?=[A-Za-z0-9!@#$%^&*_+=./~-]{6,})(?=[^\s]*[0-9!@#$%^&*_+=~])[A-Za-z0-9!@#$%^&*_+=./~-]{6,}"
    ),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "jwt": _c(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"),
    # presign URL의 서명·임시 자격증명. 외부로 공유되는 값이라 LLM 쪽으로 돌려보내지 않는다.
    "presigned_url_secret": re.compile(r"(?i)X-Amz-(?:Signature|Security-Token|Credential)=[^&\s\"']+|AWSAccessKeyId=[A-Z0-9]+"),
}

# 프롬프트 인젝션·가드 우회 시도. 사용자 입력과, 문서·도구 결과 같은 신뢰하지 않는 데이터 모두에 쓴다.
INJECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "ignore_instructions": _c(
        r"(?i)\b(?:ignore|disregard|forget|override)\b.{0,30}\b(?:previous|above|prior|earlier|all|system|your)\b.{0,20}\b(?:instructions?|prompts?|rules?|guidelines?)\b"
    ),
    "ko_ignore_instructions": re.compile(
        r"(?:이전|위|앞(?:의|선)?|기존|모든)\s*(?:모든\s*)?(?:지시|지침|명령|규칙|프롬프트)(?:을|를|사항)?\s*(?:모두\s*)?(?:무시|잊|어겨|따르지)"
    ),
    "reveal_prompt": _c(r"(?i)\b(?:reveal|show|print|repeat|leak)\b.{0,25}\b(?:system|hidden|initial)\b.{0,10}\b(?:prompt|instructions?)\b"),
    "ko_reveal_prompt": re.compile(r"시스템\s*프롬프트|숨겨진\s*(?:지시|프롬프트)|초기\s*지시"),
    "role_override": _c(r"(?i)\b(?:you are now|from now on you|act as (?:an? )?(?:unrestricted|dan)|developer mode|jailbreak)\b"),
    "ko_role_override": re.compile(r"(?:지금부터|이제부터)\s*(?:너|당신)는|너는\s*이제|제한\s*(?:없는|해제)|탈옥"),
    "fake_system_tag": re.compile(r"(?i)<\s*/?\s*(?:system|assistant|instructions?)\s*>|BEGIN\s+SYSTEM\s+PROMPT"),
    "bypass_guard": _c(
        r"(?:가드레일|가드|필터|마스킹|검증)(?:을|를|은|는)?\s*(?:끄|꺼|해제|무시|우회|비활성|건너뛰)"
        r"|승인\s*(?:없이|건너뛰|우회|생략)|4[- ]?eyes\s*(?:무시|우회|해제)"
        r"|(?i:\b(?:disable|bypass|skip|turn off)\b.{0,15}\b(?:guard(?:rail)?s?|filters?|masking|approval)\b)"
    ),
}


def find_sensitive(text: str) -> list[tuple[str, str]]:
    """(종류, 일치한 값) 목록. 비어 있으면 통과."""
    found: list[tuple[str, str]] = []
    for kind, pat in SENSITIVE_PATTERNS.items():
        for m in pat.finditer(text):
            found.append((kind, m.group(0)))
    return found


def find_secrets(text: str) -> list[str]:
    """시크릿 패턴 종류 목록(값은 돌려주지 않는다)."""
    return [kind for kind, pat in SECRET_PATTERNS.items() if pat.search(text)]


def find_injection(text: str) -> list[str]:
    """인젝션·가드 우회 패턴 종류 목록."""
    return [kind for kind, pat in INJECTION_PATTERNS.items() if pat.search(text)]
