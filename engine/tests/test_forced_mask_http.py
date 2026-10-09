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
