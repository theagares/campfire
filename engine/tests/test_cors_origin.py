"""엔진 API 를 아무 웹사이트나 읽을 수 없게 한다.

배경: 이 API 에는 인증이 없다. 예전 CORS 설정은 allow_origins=["*"] 였는데, 그건
"아무 오리진이나 이 응답을 읽어도 좋다" 고 브라우저에 **명시적으로 허락**하는 값이다.
사용자가 열어둔 아무 웹페이지나 http://127.0.0.1:4820x 로 요청을 보내 결과를 읽을 수
있다는 뜻이고, 그 결과에는 문서 원문(originalText)까지 닿는 경로가 있다.

정상 호출자는 "*" 가 필요 없다:
  - 확장: engine 을 부르는 곳은 background/service-worker.js 하나뿐이다.
  - 데스크탑: main 프로세스(Node)만 부른다 — 브라우저가 아니라 Origin 을 안 보낸다.
  - MCP 클라이언트: 브라우저가 아니다.

lifespan(모델 동기화·detector 로드)을 태우면 느리고 이 환경에서는 라우트 핸들러가
멎기까지 해서, 실제 app 은 **사전 요청(OPTIONS)** 으로만 확인한다 — 그건 미들웨어가
직접 답하고 핸들러를 타지 않는다. 세 갈래로 나눠 본다.
  1) 실제 app 이 그 정규식으로 미들웨어를 걸었는가 (배선)
  2) 같은 정규식을 건 최소 앱이 무엇을 막고 무엇을 통과시키는가 (동작 전반)
  3) 실제 app 의 사전 요청이 실제로 그렇게 답하는가 (2번이 정책을 옮겨 적으며
     틀렸을 가능성까지 막는다)

주의: 이건 **브라우저 벡터만** 닫는다. 같은 PC 의 다른 프로세스는 CORS 와 무관하게
그대로 접근할 수 있다 — 그쪽은 토큰 인증이 필요하고 별도 작업이다.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.main import EXTENSION_ORIGIN_REGEX, app

EXT_ORIGIN = "chrome-extension://" + "a" * 32


def _client_with_same_policy() -> TestClient:
    """실제 앱과 같은 CORS 정책을 건 최소 앱."""
    bare = FastAPI()
    bare.add_middleware(
        CORSMiddleware,
        allow_origin_regex=EXTENSION_ORIGIN_REGEX,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @bare.get("/probe")
    def probe():  # noqa: ANN202
        return {"ok": True}

    return TestClient(bare)


def test_real_app_uses_the_extension_origin_regex():
    """배선 확인 — 실제 app 이 와일드카드가 아니라 이 정규식을 쓴다."""
    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert cors, "CORS 미들웨어가 아예 없다"
    opts = cors[0].kwargs
    assert opts.get("allow_origin_regex") == EXTENSION_ORIGIN_REGEX
    # "*" 가 다시 들어오면 이 단정이 먼저 깨진다.
    assert "*" not in (opts.get("allow_origins") or []), "와일드카드 오리진이 되살아났다"


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "https://chatgpt.com",          # 우리가 지원하는 사이트라도 예외가 아니다
        "http://localhost:3000",
        "null",
        "chrome-extension://TOOSHORT",
    ],
)
def test_web_origins_cannot_read_the_response(origin):
    """허용 헤더가 안 나가면 브라우저가 응답 읽기를 막는다."""
    res = _client_with_same_policy().get("/probe", headers={"Origin": origin})
    assert res.headers.get("access-control-allow-origin") is None, f"{origin} 이 허용됐다"


def test_extension_origin_is_allowed():
    """확장이 막히면 제품이 통째로 멈춘다 — 이쪽은 반드시 통과해야 한다."""
    res = _client_with_same_policy().get("/probe", headers={"Origin": EXT_ORIGIN})
    assert res.headers.get("access-control-allow-origin") == EXT_ORIGIN


def test_requests_without_origin_are_untouched():
    """데스크탑 main 프로세스처럼 Origin 을 안 보내는 호출자는 영향을 받지 않는다."""
    res = _client_with_same_policy().get("/probe")
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_preflight_from_web_origin_is_rejected():
    """사전 요청(OPTIONS)에서도 웹 오리진은 허용되지 않는다."""
    res = _client_with_same_policy().options(
        "/probe",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert res.headers.get("access-control-allow-origin") is None


# ── 실제 app 으로 직접 확인 ───────────────────────────────────────────────────
# 사전 요청(OPTIONS)은 CORSMiddleware 가 직접 답하고 라우트 핸들러를 타지 않는다.
# 그래서 lifespan(모델 동기화·detector 로드) 없이도 실제 app 의 정책을 그대로 볼 수
# 있다 — 위 최소 앱 테스트가 정책을 옮겨 적으며 틀렸을 가능성까지 여기서 막는다.


@pytest.mark.parametrize("origin, allowed", [
    ("https://evil.example", False),
    ("https://chatgpt.com", False),
    (EXT_ORIGIN, True),
])
def test_real_app_preflight(origin, allowed):
    res = TestClient(app).options(
        "/health",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    got = res.headers.get("access-control-allow-origin")
    assert (got == origin) is allowed, f"{origin} -> {got}"
