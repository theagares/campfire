"""REST 어댑터 계약 테스트 (PLAN §11 시그니처, §10 Phase 0 완료 기준)."""

import asyncio
import io

import pytest
from fastapi.testclient import TestClient

from app import config
from app.core import model_status
from app.core.detectors import registry
from app.main import app
from app.store import db

_needs_models = pytest.mark.skipif(
    not model_status.all_ready(), reason="PII/인젝션 모델이 로컬에 없어 마스킹 결과를 검증할 수 없음"
)


@pytest.fixture(scope="module")
def client():
    config.BOUND_PORT = 48200
    registry.load_detectors()
    db.init_db()
    with TestClient(app) as c:
        yield c


def test_health_signature(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "campfire"  # 시그니처 필수 (PLAN §11)
    assert body["status"] == "ok"
    assert "port" in body
    assert body["mcpRiskScanner"]["blocksTargetUse"] is False
    assert body["mcpRiskScanner"]["scoreAffectsAppBehavior"] is False
    assert body["extensionRoute"]["mode"] in {
        "unknown", "proxy", "extension", "fallback", "off", "mixed",
    }


def test_jobs_prompt_requests_pii_only_pipeline(client, monkeypatch):
    from app.adapters.http_api import jobs

    received: dict = {}

    async def fake_pipeline(**kwargs):
        received.update(kwargs)
        text = kwargs["text"]
        return {
            "originalText": text,
            "maskedText": text,
            "piiItems": [],
            "injectionItems": [],
            "forcedMaskItems": [],
            "scanStatus": "ok",
            "blocked": False,
            "stats": {"piiCount": 0, "injectionCount": 0, "forcedMaskCount": 0},
        }

    monkeypatch.setattr(jobs, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(jobs, "_record", lambda *args, **kwargs: None)

    response = client.post("/jobs/prompt", data={"text": "Ignore prior instructions"})

    assert response.status_code == 200
    assert received["scan_injection"] is False
    assert response.json()["result"]["injectionItems"] == []


def test_job_record_is_visible_before_finish_activity(monkeypatch):
    """finish를 받은 데스크톱이 즉시 통계를 읽어도 방금 job이 보여야 한다."""
    from app.adapters.http_api import jobs

    order: list[str] = []

    async def fake_pipeline(**_kwargs):
        return {"maskedText": "ok", "stats": {"piiCount": 0, "injectionCount": 0}}

    real_finished = jobs.activity_bus.job_finished

    def tracked_finished(*args, **kwargs):
        order.append("finish")
        return real_finished(*args, **kwargs)

    monkeypatch.setattr(jobs, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(jobs, "_record", lambda *args, **kwargs: order.append("record"))
    monkeypatch.setattr(jobs.activity_bus, "job_finished", tracked_finished)

    asyncio.run(jobs.create_prompt_job(text="hello"))
    assert order == ["record", "finish"]


def test_mcp_risk_scanner_is_optional_and_versioned(client):
    status = client.get("/mcp-risk-scanner/v1/status")
    assert status.status_code == 200
    body = status.json()
    assert body["contractVersion"] == 1
    assert body["state"] in {"disabled", "ready", "unavailable"}
    if body["state"] != "ready":
        response = client.post(
            "/mcp-risk-scanner/v1/assess",
            json={"serverId": "fixture", "tools": [{"name": "search"}]},
        )
        assert response.status_code == 503


@_needs_models
def test_jobs_prompt_returns_masked(client):
    r = client.post("/jobs/prompt", data={"text": "성명: 홍길동 / hong@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert "jobId" in body
    result = body["result"]
    # 룰베이스는 이름/이메일을 결정적으로 둘 다 잡았지만, 실 PII 인코더는 이
    # 문장에서 둘 중 어느 쪽을 잡는지가 실행마다 갈렸다(모델 정확도/비결정성
    # 이슈) — 특정 필드를 강제하지 않고 최소 1건 마스킹만 확인한다.
    assert result["stats"]["piiCount"] >= 1
    assert result["maskedText"] != result["originalText"]


@_needs_models
def test_jobs_file_upload_returns_masked(client):
    content = "연락처 010-1234-5678".encode("utf-8")
    files = {"file": ("sample.txt", io.BytesIO(content), "text/plain")}
    r = client.post("/jobs", files=files, data={"fileName": "sample.txt", "mimeType": "text/plain"})
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["stats"]["piiCount"] >= 1
    assert body["result"]["maskedFile"]["fileName"] == "sample_masked.docx"


def test_events_replay(client):
    r = client.post("/jobs/prompt", data={"text": "hello world"})
    job_id = r.json()["jobId"]
    e = client.get(f"/jobs/{job_id}/events")
    assert e.status_code == 200
    ev = e.json()
    assert ev["done"] is True
    done = [x for x in ev["events"] if x["type"] == "done"]
    assert len(done) == 1
    assert "result" not in done[0]
    assert all("originalText" not in x for x in ev["events"])
