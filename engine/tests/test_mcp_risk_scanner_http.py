"""HTTP contract stays thin and independent from scanner implementation modules."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.http_api.mcp_risk_scanner import router


class _FakeScanner:
    def status(self):
        return {
            "contractVersion": 1,
            "enabled": True,
            "state": "ready",
            "transport": "stdio-mcp",
            "blocksTargetUse": False,
            "scoreAffectsAppBehavior": False,
        }

    async def assess_snapshot(self, **kwargs):
        return {
            "serverId": kwargs["server_id"],
            "riskScore": 0,
            "securityScore": 100,
            "coverage": {"tool_definitions": "checked"},
            "findings": [],
        }


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.mcp_risk_scanner = _FakeScanner()
    return TestClient(app)


def test_assess_contract_delegates_through_port():
    with _client() as client:
        response = client.post(
            "/mcp-risk-scanner/v1/assess",
            json={"serverId": "fixture", "tools": [{"name": "search"}]},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["contractVersion"] == 1
    assert body["available"] is True
    assert body["report"]["securityScore"] == 100


def test_http_boundary_does_not_expose_local_source_paths():
    with _client() as client:
        response = client.post(
            "/mcp-risk-scanner/v1/assess",
            json={"serverId": "fixture", "tools": [], "sourcePath": "C:/private"},
        )
    assert response.status_code == 422
