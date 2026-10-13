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
    fresh = proxy_control.extension_route_status(now=111.9)
    assert fresh["mode"] == "extension"

    stale = proxy_control.extension_route_status(now=112.1)
    assert stale["mode"] == "unknown"
    assert stale["connected"] is False
