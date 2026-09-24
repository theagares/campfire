"""토글 REST — 앱이 부르는 켜기/끄기/상태.

실제 mitmproxy 를 띄운다. 엔진 lifespan 은 모델을 올리므로 라우터만 얹은 작은
앱으로 돈다(`with TestClient` 로 요청 사이에 같은 이벤트 루프를 유지해야 프록시
태스크가 살아 있다).
"""

from __future__ import annotations

import socket

import pytest

pytest.importorskip("mitmproxy")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.adapters.http_api import proxy_control  # noqa: E402

PORT = 48218


def _bound() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "PROXY_PORT", PORT)
    app = FastAPI()
    app.include_router(proxy_control.router)
    with TestClient(app) as c:
        yield c
        c.post("/proxy/stop")  # 실패해도 포트를 남기지 않는다


def test_상태는_늘_같은_모양이다(client):
    s = client.get("/proxy/status").json()
    for key in ("supported", "running", "port", "caPath", "caExists", "caTrusted"):
        assert key in s, key
    assert s["running"] is False
    assert s["port"] == PORT


def test_켜고_끄고_다시_켠다(client):
    on = client.post("/proxy/start").json()
    assert on["running"] is True
    assert _bound()

    off = client.post("/proxy/stop").json()
    assert off["running"] is False
    assert not _bound(), "껐는데 포트가 남았다 — 다시 켤 수 없게 된다"

    again = client.post("/proxy/start").json()
    assert again["running"] is True, "두 번째 켜기 실패"


def test_두번_켜도_하나만_뜬다(client):
    assert client.post("/proxy/start").json()["running"] is True
    assert client.post("/proxy/start").json()["running"] is True
    assert client.post("/proxy/stop").json()["running"] is False
    assert not _bound()
