"""자격증명이 게이트웨이를 그냥 통과하던 문제를 지킨다.

왜 생겼나 (2026-09-06 실측):
    엔진에 아래를 넣으면 piiItems 가 0건이고 maskedText 가 입력 그대로였다.

        AWS_SECRET_ACCESS_KEY=...   GITHUB_TOKEN=ghp_...
        -----BEGIN RSA PRIVATE KEY-----   password: hunter2

    PII 모델의 라벨 체계에 자격증명이 없으니 당연한 결과다. 문제는 이 게이트웨이를
    통과한 텍스트가 그대로 외부 AI 서비스로 나간다는 것이고, MCP 의 secure_read_file
    은 "원본이 파이프라인을 우회해 새어나가는 경로를 없앤다" 고 설명한다는 것이다.
    .env 나 개인키를 읽히면 키가 그대로 나가는데 사용자는 걸러졌다고 믿는다.

이 파일이 지키는 것:
    (1) 대표적인 자격증명 형태를 놓치지 않는다(재현율)
    (2) 평범한 문서와 자리표시자를 가리지 않는다(정밀도)
    (3) key=value 는 **값만** 가린다 — 키 이름까지 지우면 무슨 설정인지 안 보여서
        사용자가 마스킹 해제 여부를 판단할 수 없다

모델이 필요 없으므로 가중치 없이도 돌아간다.

실행: pytest engine/tests/test_credentials.py
"""

from __future__ import annotations

import pytest

from app.core.detectors.base import CREDENTIAL
from app.core.detectors.pii import credentials
from app.core.masker import masker


def _masked(text: str) -> str:
    return masker.apply_masking(text, credentials.detect(text))["masked_text"]


# ── (1) 재현율 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name, text",
    [
        ("aws_access_key_id", "AKIAIOSFODNN7EXAMPLE"),
        ("aws_secret", "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
        ("github_token", "GITHUB_TOKEN=ghp_16C7e42F292c6912E7710c838347Ae178B4a"),
        ("openai_key", "sk-proj-abcdefghijklmnopqrstuvwxyz1234"),
        ("slack_token", "xoxb-123456789012-abcdefghijkl"),
        ("google_api_key", "AIzaSyDaGmWKa4JsXZ-HjGw7ISLn_3namBGewQe"),
        ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"),
        ("password", "password: hunter2"),   # 7자 — 실제 비밀번호로 흔한 길이
        ("client_secret", 'client_secret="Abcd1234EfghIjkl"'),
        ("lowercase_api_key", "my_api_key = 8sdf9sdf7sdfsdf"),
    ],
)
def test_credential_is_detected(name, text):
    items = credentials.detect(text)
    assert items, f"{name} 을 놓쳤다 — 이 값이 그대로 외부 AI 서비스로 나간다"
    assert all(i["type"] == CREDENTIAL for i in items)


def test_pem_block_is_masked_whole(  ):
    """개인키는 헤더부터 푸터까지 한 덩어리로 가린다 — 본문만 가리면 의미가 없다."""
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF0qJ\n"
        "-----END RSA PRIVATE KEY-----"
    )
    assert "MIIEow" not in _masked(text)
    assert "BEGIN RSA PRIVATE KEY" not in _masked(text)


# ── (2) 정밀도 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name, text",
    [
        ("평범한 한국어", "이 토큰: 다음 단계로 넘어갑니다"),
        ("자리표시자 your-", "API_KEY=your-api-key-here"),
        ("자리표시자 꺾쇠", "password: <YOUR_PASSWORD>"),
        ("자리표시자 changeme", "SECRET_KEY=changeme"),
        ("환경변수 참조", "token=${GITHUB_TOKEN}"),
        ("너무 짧은 값", "token: abc"),
        ("코드의 환경변수 읽기", "const apiKey = process.env.OPENAI_API_KEY"),
        ("코드의 환경변수 읽기(py)", "api_key = os.environ['OPENAI_API_KEY']"),
        ("PII 만 있는 문서", "홍길동 010-1234-5678 hong@example.com"),
    ],
)
def test_not_a_credential(name, text):
    assert credentials.detect(text) == [], f"{name} 을 자격증명으로 오탐했다"


# ── (3) key=value 는 값만 ────────────────────────────────────────────────────
def test_key_name_survives_masking():
    """키 이름이 남아야 사용자가 무슨 설정인지 보고 판단할 수 있다."""
    out = _masked("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    assert out.startswith("AWS_SECRET_ACCESS_KEY=")
    assert "wJalrXUtnFEMI" not in out


def test_same_secret_is_not_counted_twice():
    """GITHUB_TOKEN=ghp_... 는 통째 패턴과 key=value 패턴에 모두 걸린다.

    두 번 세면 사용자에게 같은 항목이 두 줄로 보이고 마스킹도 겹친다.
    """
    text = "GITHUB_TOKEN=ghp_16C7e42F292c6912E7710c838347Ae178B4a"
    assert len(credentials.detect(text)) == 1


def test_multiple_secrets_in_one_document():
    text = (
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
        "GITHUB_TOKEN=ghp_16C7e42F292c6912E7710c838347Ae178B4a\n"
        "password: hunter2\n"
    )
    out = _masked(text)
    for secret in ("wJalrXUtnFEMI", "ghp_16C7e42F292c6912E7710c838347Ae178B4a", "hunter2"):
        assert secret not in out, f"{secret} 이 마스킹되지 않았다"


# ── 리뷰 후속(#148 롤업 리뷰) — 미탐 3종과 성능 ──────────────────────────────
@pytest.mark.parametrize(
    "name, text, secret",
    [
        # 키가 따옴표로 감싸이면 통째로 미탐이었다. MCP 파일 도구가 읽는 자격증명
        # 파일(서비스 계정 json, config.json, API 응답)은 대부분 이 형태다.
        ("json api_key", '{"api_key": "sk_live_abcdef123456"}', "sk_live_abcdef123456"),
        ("json password", '{"password": "hunter2xyz"}', "hunter2xyz"),
        ("json 작은따옴표", "{'access_key': 'AbCdEf123456'}", "AbCdEf123456"),
        # 자리표시자 접두 매칭 때문에 your/my/the/some/any 로 "시작하는" 진짜 값이
        # 통째로 버려졌다. 구분자가 없으면 자리표시자가 아니다.
        ("my 로 시작하는 실제 값", "password=mysecret123", "mysecret123"),
        ("the 로 시작하는 실제 값", "api_key=theRealKey99", "theRealKey99"),
        ("some 로 시작하는 실제 값", "token=someRandomTokenValue", "someRandomTokenValue"),
        ("any 로 시작하는 실제 값", "password=anyThing123", "anyThing123"),
    ],
)
def test_previously_missed_credential_is_detected(name, text, secret):
    assert secret not in _masked(text), f"{name} 이 마스킹되지 않았다 — 그대로 외부로 나간다"


def test_quoted_value_with_spaces_is_masked_to_the_closing_quote():
    """따옴표로 연 값은 공백이 있어도 끝까지 가린다.

    값 패턴이 공백에서 끊겨서 첫 단어만 가려지고 나머지가 남았다 — 패스프레이즈와
    connection string 이 뒷부분을 그대로 흘렸다.
    """
    out = _masked('db_password = "correct horse battery"')
    for part in ("correct", "horse", "battery"):
        assert part not in out, f"'{part}' 이 남았다: {out}"
    assert out.startswith("db_password = "), "키 이름은 남아야 한다"


def test_key_value_scan_is_linear():
    """단어문자가 끊기지 않는 긴 런에서 백트래킹이 터지지 않는다.

    식별자 꼬리가 `*` 였을 때 40KB 한 덩어리에 2.9초가 걸렸다(n²). detect() 는
    async _detect_pii 안에서 **동기로** 불리므로 그동안 이벤트 루프가 멎고
    /health·SSE 가 같이 멈춘다.
    """
    import time

    start = time.perf_counter()
    credentials.detect("token" * 40_000)   # 200KB = config.MAX_TEXT_CHARS 상한
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"200KB 스캔에 {elapsed:.2f}초 — 백트래킹이 터졌다"
