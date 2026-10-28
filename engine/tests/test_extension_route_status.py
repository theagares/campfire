"""Active browser route reporting used by the desktop/extension status UI."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.http_api import proxy_control

EXT_ORIGIN = "chrome-extension://" + "a" * 32


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(proxy_control.router)
    return TestClient(app)


def setup_function() -> None:
    proxy_control._extension_routes.clear()


def teardown_function() -> None:
    proxy_control._extension_routes.clear()


def test_route_report_requires_extension_origin() -> None:
    response = _client().post("/proxy/extension-route", json={
        "mode": "proxy", "host": "chatgpt.com", "tabId": 3, "expectedProxy": True,
    })
    assert response.status_code == 403


def test_route_reports_show_verified_and_mixed_tabs() -> None:
    client = _client()
    first = client.post("/proxy/extension-route", headers={"Origin": EXT_ORIGIN}, json={
        "mode": "proxy", "host": "ChatGPT.com", "tabId": 3, "expectedProxy": True,
    })
    assert first.status_code == 200
    assert first.json()["mode"] == "proxy"
    assert first.json()["activeTabs"] == 1

    second = client.post("/proxy/extension-route", headers={"Origin": EXT_ORIGIN}, json={
        "mode": "fallback", "host": "claude.ai", "tabId": 4, "expectedProxy": True,
    })
    body = second.json()
    assert body["mode"] == "mixed"
    assert body["activeTabs"] == 2
    assert body["counts"] == {"proxy": 1, "extension": 0, "fallback": 1, "off": 0}
    assert {site["host"] for site in body["sites"]} == {"chatgpt.com", "claude.ai"}


def test_stale_route_reports_expire() -> None:
    proxy_control._extension_routes[(EXT_ORIGIN, 7, "gemini.google.com")] = {
        "mode": "extension",
        "host": "gemini.google.com",
        "tabId": 7,
        "expectedProxy": False,
        "seenAt": 100.0,
    }
    ttl = proxy_control._ROUTE_TTL_SECONDS
    fresh = proxy_control.extension_route_status(now=100.0 + ttl - 0.1)
    assert fresh["mode"] == "extension"

    stale = proxy_control.extension_route_status(now=100.0 + ttl + 0.1)
    assert stale["mode"] == "unknown"
    assert stale["connected"] is False


def test_tabs_on_sites_the_proxy_does_not_cover_do_not_make_it_mixed() -> None:
    """copilot.com 은 프록시가 맡지 않아 켜져 있어도 확장이 맡는다 — 그 탭 때문에 "탭별 경로 다름" 이면 안 된다(Windows PX-7)."""
    def put(tab: int, host: str, mode: str) -> None:
        proxy_control._extension_routes[(EXT_ORIGIN, tab, host)] = {
            "mode": mode, "host": host, "tabId": tab, "expectedProxy": True, "seenAt": 100.0,
        }

    put(1, "chatgpt.com", "proxy")
    put(2, "copilot.com", "fallback")
    status = proxy_control.extension_route_status(now=101.0)
    assert status["mode"] == "proxy"
    assert status["counts"]["fallback"] == 1 and len(status["sites"]) == 2  # 목록·숫자에는 남긴다

    put(3, "claude.ai", "fallback")  # 프록시가 맡는 사이트가 어긋나면 여전히 다름이다
    assert proxy_control.extension_route_status(now=101.0)["mode"] == "mixed"

    proxy_control._extension_routes.clear()
    put(2, "copilot.com", "fallback")  # 그런 탭만 있으면 그대로 보인다
    assert proxy_control.extension_route_status(now=101.0)["mode"] == "fallback"


def test_idle_tab_keeps_route_well_past_old_12s_window() -> None:
    """확장은 탭 전환·클릭·Enter 때만 보고한다 — 가만히 둔 탭이 12초 만에 "확인 대기" 로 떨어지면 안 된다(맥 실측)."""
    proxy_control._extension_routes[(EXT_ORIGIN, 3, "chatgpt.com")] = {
        "mode": "proxy", "host": "chatgpt.com", "tabId": 3, "expectedProxy": True, "seenAt": 100.0,
    }
    assert proxy_control.extension_route_status(now=100.0 + 120)["mode"] == "proxy"


def test_proxy_start_and_stop_clear_old_route_reports(monkeypatch) -> None:
    async def noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(proxy_control.proxy, "start", noop)
    monkeypatch.setattr(proxy_control.proxy, "stop", noop)
    monkeypatch.setattr(proxy_control.proxy, "status", lambda: {"running": False})
    client = _client()
    report = {"mode": "proxy", "host": "chatgpt.com", "tabId": 3, "expectedProxy": True}
    for path in ("/proxy/stop", "/proxy/start"):
        assert client.post("/proxy/extension-route", headers={"Origin": EXT_ORIGIN}, json=report).json()["mode"] == "proxy"
        body = client.post(path).json()
        assert body["extensionRoute"]["mode"] == "unknown", path  # 켜고 끈 뒤에는 탭이 다시 보고해야 보인다
