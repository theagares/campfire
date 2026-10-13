"""/models/fetch — 기밀 탐지 리트리버는 기밀 탐지를 켠 경우에만, 없을 때만 받는다 (실제 다운로드 없이 흐름만)."""

from __future__ import annotations

import asyncio

import pytest

from app import config
from app.adapters.http_api import models
from app.core import model_status


@pytest.fixture
def fetched(monkeypatch):
    got: list[str] = []
    events: list[dict] = []

    async def fake_download(name, spec, emit):
        got.append(name)

    async def emit(ev):
        events.append(ev)

    monkeypatch.setattr(models, "_download_and_extract", fake_download)
    monkeypatch.setattr(models, "_pii_weights_present", lambda: True)
    monkeypatch.setattr(models, "_injection_head_present", lambda: True)
    monkeypatch.setattr(models.backbone, "is_cached", lambda: True)
    monkeypatch.setattr(models.job_registry, "make_emit", lambda _job: emit)
    return got, events


@pytest.mark.parametrize("enabled, ready, want", [(True, False, ["confidential"]), (True, True, []), (False, False, [])])
def test_기밀_리트리버는_켜져_있고_없을_때만_받는다(fetched, monkeypatch, enabled, ready, want):
    got, events = fetched
    monkeypatch.setattr(config, "CONFIDENTIAL_ENABLED", enabled)
    monkeypatch.setattr(model_status, "confidential_ready", lambda: ready)
    asyncio.run(models._fetch_job("job"))
    assert got == want
    assert events[-1]["type"] == "done"
    assert events[-1]["result"]["confidential"] == {"enabled": enabled, "ready": ready}


def test_기밀_리트리버_자산은_고정된_체크섬과_위치를_쓴다():
    spec = models._ASSETS["confidential"]
    assert spec["url"].endswith("/models-v1/confidential_engine_v1.tar.gz")
    assert len(spec["sha256"]) == 64
    assert spec["extract_to"]() == config.CONFIDENTIAL_ENGINE_DIR
