"""작업 진행 이벤트가 결과 본문처럼 무제한으로 메모리에 남지 않는지 검증한다."""

import pytest

from app.adapters.http_api import job_registry


@pytest.fixture(autouse=True)
def _empty_registry():
    job_registry._jobs.clear()
    yield
    job_registry._jobs.clear()


def test_completed_job_expires(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(job_registry.time, "time", lambda: now[0])
    monkeypatch.setattr(job_registry, "_COMPLETED_TTL_SECONDS", 60.0)

    job_registry.create_job("done-job")
    job_registry.record_event("done-job", {"type": "done"})
    assert job_registry.has_job("done-job")

    now[0] = 160.0
    assert not job_registry.has_job("done-job")


def test_event_count_and_bytes_are_bounded(monkeypatch):
    monkeypatch.setattr(job_registry, "_MAX_EVENTS_PER_JOB", 3)
    monkeypatch.setattr(job_registry, "_MAX_EVENT_BYTES_PER_JOB", 220)
    job_registry.create_job("bounded-job")

    for index in range(10):
        job_registry.record_event(
            "bounded-job", {"type": "step", "label": f"{index}:" + ("x" * 80)}
        )

    events = job_registry.get_events("bounded-job")
    assert 1 <= len(events) <= 3
    assert events == sorted(events, key=lambda event: event["seq"])
    assert events[-1]["seq"] == 10
    assert job_registry._jobs["bounded-job"]["event_bytes"] <= 220


def test_single_oversized_event_drops_result_payload(monkeypatch):
    monkeypatch.setattr(job_registry, "_MAX_EVENT_BYTES_PER_JOB", 256)
    job_registry.create_job("large-result")

    stored = job_registry.record_event(
        "large-result", {"type": "done", "result": {"originalText": "x" * 10_000}}
    )

    assert stored["type"] == "done"
    assert stored["truncated"] is True
    assert "result" not in stored
    assert job_registry._jobs["large-result"]["event_bytes"] <= 256
