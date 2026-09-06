"""자격증명 탐지 — 정규식 전용, 모델 불필요.

왜 필요한가 (2026-09-06 실측):
    이 게이트웨이를 통과한 텍스트는 그대로 외부 AI 서비스로 나간다. 그런데 PII 모델의
    라벨 체계에는 자격증명이 없다(개인정보가 아니니 당연하다). 실제로 넣어보면 이렇다.

        piiItems: []
        maskedText: AWS_SECRET_ACCESS_KEY=...  GITHUB_TOKEN=ghp_...
                    -----BEGIN RSA PRIVATE KEY-----  password: hunter2

    입력이 그대로 나온다. MCP 의 secure_read_file 은 "원본이 파이프라인을 우회해
    새어나가는 경로를 없앤다" 고 설명하는데, .env 나 개인키를 읽히면 키가 그대로
    에이전트에게 전달된다 — 사용자는 게이트를 통과했으니 안전하다고 믿는다.

왜 정규식인가:
    자격증명은 개인정보와 달리 **형태가 고정된** 범주다. 접두사(ghp_, AKIA, sk-)와
    구분자가 규격이라 정규식의 정밀도가 높고, 모델과 달리 재현율이 문장 형태에
    흔들리지 않는다. 주소(#143)가 NER 판단에 의존해 미탐이 난 것과 정반대다.

오탐에 대한 태도:
    보안 게이트웨이에서 자격증명을 놓치는 비용(키 유출)이 잘못 가리는 비용(사용자가
    한 항목 마스킹을 해제)보다 훨씬 크다. 그래서 애매하면 가리는 쪽으로 기운다.
    다만 key=value 형태에서는 **값만** 잡는다 — 키 이름까지 가리면 무슨 설정인지조차
    안 보여서 사용자가 판단할 수 없다.
"""

from __future__ import annotations

import re

from app.core.detectors.base import CREDENTIAL, Detection

# 정규식 신뢰도. 형태가 규격이라 모델 탐지보다 확신이 높지만, 사용자가 해제할 수
# 있어야 하므로 1.0 은 쓰지 않는다.
_CONFIDENCE = 0.95

# ── 통째로 가리는 패턴 (그 자체가 곧 비밀) ────────────────────────────────────
#
# 값만 잡는 게 아니라 매치 전체를 가린다. 접두사만 봐도 무엇인지 드러나고,
# 접두사를 남겨두면 어떤 서비스의 키가 여기 있었는지가 그대로 노출된다.
_WHOLE_MATCH: tuple[tuple[str, re.Pattern[str]], ...] = (
    # PEM 블록 — 헤더부터 푸터까지 통째로. DOTALL 로 여러 줄을 한 덩어리로 본다.
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    )),
    # AWS 액세스 키 ID
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # GitHub 토큰 (ghp_ 개인, gho_ OAuth, ghu_/ghs_ 앱, ghr_ 갱신)
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    # OpenAI / Anthropic 계열
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    # Slack
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # Google API 키
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    # JWT — 헤더.페이로드.서명
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)

# ── 값만 가리는 패턴 (key=value) ──────────────────────────────────────────────
#
# 키 이름은 남긴다. AWS_SECRET_ACCESS_KEY=[자격증명 마스킹] 이면 사용자가 무슨
# 설정인지 알아보고 판단할 수 있다. 통째로 가리면 그 판단이 불가능하다.
#
# 값 길이 하한(6자)이 오탐 방어의 핵심이다. "token: 다음" 같은 평범한 한국어 문장이
# 걸리지 않게 한다. 따옴표는 값에서 제외한다 — 가린 뒤에도 따옴표가 남아야 원래
# 형태를 알아볼 수 있다.
_KEY_VALUE = re.compile(
    r"""(?ix)
    # 키 이름은 "키워드를 포함한 식별자" 로 본다. (?:secret) 로 쓰면
    # AWS_SECRET_ACCESS_KEY 가 안 걸린다 — '_' 가 단어문자라 AWS_ 와 SECRET 사이에
    # 단어 경계가 없기 때문이다(실측 미탐).
    (?: ^ | [^\w.-] )
    [\w.-]*
    (?: password | passwd | passphrase
      | secret | api[_-]?key | access[_-]?key
      | auth[_-]?token | token | bearer
      | credential )
    [\w.-]*
    \s* [:=] \s*
    ['"]?
    (?P<value> [^\s'"]{6,} )
    """,
    re.MULTILINE,
)

# 값처럼 보이지만 비밀이 아닌 것들. 예제/자리표시자를 가리면 문서만 읽기 어려워진다.
_PLACEHOLDER = re.compile(
    r"""(?ix) ^(?:
        (?: your | my | the | some | any )[\w-]* |
        x{3,} | \*{3,} | \.{3,} |
        <[^>]*> | \{\{?[^}]*\}?\} | \$\{[^}]*\} |
        change[_-]?me | replace[_-]?me | example\w* | dummy\w* | sample\w* |
        none | null | true | false | undefined |
        # 코드에서 흔한 참조 — 값이 아니라 '어디서 읽어오는지' 다.
        process\.env[\w.\[\]'"-]* | os\.environ[\w.\[\]'"-]* | env\.[\w.]+
    )$""",
)


def _add(out: list[Detection], text: str, start: int, end: int, seen: set[tuple[int, int]]) -> None:
    if start >= end or (start, end) in seen:
        return
    seen.add((start, end))
    out.append(
        Detection(
            type=CREDENTIAL,
            start=start,
            end=end,
            text=text[start:end],
            confidence=_CONFIDENCE,
            source="rule",
        )
    )


def detect(text: str) -> list[Detection]:
    """text 에서 자격증명으로 보이는 구간을 찾는다.

    모델을 쓰지 않으므로 동기 함수다 — 모델이 준비되지 않은 상태에서도 돌 수 있다.
    """
    if not text:
        return []

    out: list[Detection] = []
    seen: set[tuple[int, int]] = set()

    for _name, pattern in _WHOLE_MATCH:
        for m in pattern.finditer(text):
            _add(out, text, m.start(), m.end(), seen)

    for m in _KEY_VALUE.finditer(text):
        value = m.group("value")
        if _PLACEHOLDER.match(value):
            continue
        # 이미 통째로 잡힌 토큰의 값 부분이면 건너뛴다 — 같은 비밀을 두 번 세지 않는다.
        vs, ve = m.span("value")
        if any(s <= vs and ve <= e for s, e in seen):
            continue
        _add(out, text, vs, ve, seen)

    return out
