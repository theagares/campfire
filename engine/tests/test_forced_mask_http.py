from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.adapters.http_api import forced_mask_rules
from app.core import forced_mask


def _client():
    app = FastAPI()
    app.include_router(forced_mask_rules.router)
    return TestClient(app)


def test_internal_rule_update_requires_per_launch_token(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_CONTROL_TOKEN", "test-token")
    client = _client()
    assert client.put("/internal/forced-mask-rules", json={"terms": ["secret"]}).status_code == 404
    assert client.put(
        "/internal/forced-mask-rules",
        headers={"Authorization": "Bearer wrong"},
        json={"terms": ["secret"]},
    ).status_code == 404


def test_internal_rule_update_returns_metadata_not_terms(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_CONTROL_TOKEN", "test-token")
    client = _client()
    response = client.put(
        "/internal/forced-mask-rules",
        headers={"Authorization": "Bearer test-token"},
        json={"terms": ["Project Aurora", "project aurora"]},
    )
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert "terms" not in response.json()
    assert forced_mask.registry.snapshot().find("PROJECT AURORA")


def test_internal_rule_update_rejects_invalid_terms(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_CONTROL_TOKEN", "test-token")
    response = _client().put(
        "/internal/forced-mask-rules",
        headers={"Authorization": "Bearer test-token"},
        json={"terms": ["line\nbreak"]},
    )
    assert response.status_code == 422


def test_unauthenticated_requests_never_reveal_the_endpoint(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_CONTROL_TOKEN", "test-token")
    client = _client()
    url = "/internal/forced-mask-rules"
    # 본문이 틀려도 인증이 먼저다 — 422 가 나오면 엔드포인트가 있다는 게 드러난다.
    assert client.put(url, json={"terms": "x"}).status_code == 404
    assert client.put(url, content=b"{not json").status_code == 404
    # Bearer 접두사 없이 토큰만 보내도, 비ASCII 헤더를 보내도 404(500 아님).
    assert client.put(url, headers={"Authorization": "test-token"}, json={"terms": []}).status_code == 404
    assert client.put(url, headers={"Authorization": "Bearer tést".encode("latin-1")},
                      json={"terms": []}).status_code == 404
