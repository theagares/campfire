"""처리현황 실시간 방송(core.activity + /activity*) 테스트.

대시보드가 "지금 무슨 단계가 돌고 있는지" 를 볼 수 있어야 한다는 계약을 지킨다.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import config
from app.core import activity as activity_bus
from app.core import model_status
from app.core.detectors import registry
from app.main import app
from app.store import db

_needs_models = pytest.mark.skipif(
    not model_status.all_ready(),
    reason="PII/인젝션 모델 가중치가 로컬에 없어 실 모델 경로를 검증할 수 없음",
)



@pytest.fixture(scope="module")
def client():
    config.BOUND_PORT = 48200
    registry.load_detectors()
    db.init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_bus():
    activity_bus.reset()
    yield
    activity_bus.reset()


def test_activity_idle_when_nothing_running(client):
    r = client.get("/activity")
    assert r.status_code == 200
    assert r.json() == {"active": [], "busy": False}


@_needs_models
def test_job_publishes_stages_and_clears(client):
    """실제 job 을 돌리면 단계가 방송되고, 끝나면 진행 목록이 비어야 한다."""
    events: list[dict] = []
    q = activity_bus.subscribe()

    r = client.post("/jobs/prompt", data={"text": "홍길동 010-1234-5678"})
    assert r.status_code == 200

    while not q.empty():
        events.append(q.get_nowait())
    activity_bus.unsubscribe(q)

    phases = [e["phase"] for e in events]
    stages = [e["stage"] for e in events]

    assert phases[0] == "start" and stages[0] == "receive", "job 시작이 먼저 방송돼야 한다"
    assert "parse" in stages, "파싱 단계가 방송돼야 한다"
    assert "pii" in stages, "PII 탐지 단계가 방송돼야 한다"
    assert "injection" in stages, "인젝션 탐지 단계가 방송돼야 한다"
    assert phases[-1] == "finish", "마지막은 완료 방송이어야 한다"
    assert events[-1]["ok"] is True

    # 끝난 job 이 진행 목록에 남아 "영원히 탐지중" 이 되면 안 된다.
    assert activity_bus.snapshot() == []
    assert client.get("/activity").json()["busy"] is False


def test_model_fetch_does_not_pollute_activity():
    """/models/fetch 도 같은 job 이벤트 구조를 쓰지만 탐지가 아니므로 방송되면 안 된다."""
    from app.adapters.http_api import job_registry

    q = activity_bus.subscribe()
    emit = job_registry.make_emit("download-job")  # activity 옵트인 안 함
    asyncio.run(emit({"type": "progress", "asset": "pii", "pct": 12.0}))
    asyncio.run(emit({"type": "done", "result": {}}))
    activity_bus.unsubscribe(q)

    assert q.empty(), "모델 다운로드 진행률이 처리현황 방송에 섞이면 안 된다"
    assert activity_bus.snapshot() == []


def test_slow_subscriber_does_not_block_producer():
    """구독자 큐가 차도 방송이 producer 를 막지 않고, 최신 이벤트는 계속 들어와야 한다."""
    q = activity_bus.subscribe()
    for i in range(500):  # _QUEUE_MAX(64) 를 훨씬 넘겨 밀어넣는다
        activity_bus.job_started(f"job-{i}")
    activity_bus.unsubscribe(q)

    assert q.qsize() <= 64, "구독자 큐는 상한을 지켜야 한다"
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    # 오래된 것이 버려지고 최신이 남는다
    assert drained[-1]["jobId"] == "job-499"


def test_snapshot_lets_late_subscriber_catch_up():
    """처리 도중 접속한 구독자도 현재 진행 상태를 알 수 있어야 한다."""
    activity_bus.job_started("in-flight", source="extension")
    activity_bus.job_event("in-flight", {"type": "step", "step": 2, "label": "PII 탐지 중..."})

    snap = activity_bus.snapshot()
    assert len(snap) == 1
    assert snap[0]["jobId"] == "in-flight"
    assert snap[0]["stage"] == "pii"
    assert snap[0]["source"] == "extension"

    activity_bus.job_finished("in-flight", ok=True)
    assert activity_bus.snapshot() == []


def test_failed_job_clears_busy_state():
    """실패한 job 이 방송에서 안 끝나면 대시보드가 영원히 '탐지중' 으로 남는다."""
    activity_bus.job_started("boom")
    activity_bus.job_event("boom", {"type": "error", "message": "파싱 실패"})
    assert activity_bus.snapshot() == []


def test_activity_stream_route_is_registered(client):
    """SSE 엔드포인트가 앱에 실제로 붙어 있는지 — 무한 스트림이라 직접 열어 확인할 수
    없으므로(열면 테스트가 멈춘다) OpenAPI 스키마로 확인한다."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/activity" in paths
    assert "/activity/stream" in paths


def test_sse_sends_snapshot_then_live_events():
    """SSE 제너레이터를 직접 돌린다 — TestClient 로 무한 스트림을 읽으면 스트림이
    닫히지 않아 테스트가 멈춘다(실측). 계약만 확인하면 되므로 제너레이터로 검증한다."""
    import json

    from app.adapters.http_api.activity import _sse

    async def scenario():
        activity_bus.job_started("streaming-job", source="prompt")
        gen = _sse()
        try:
            first = json.loads((await anext(gen))[len("data: ") :])

            # 구독이 붙은 뒤 발생한 이벤트는 라이브로 흘러야 한다.
            activity_bus.job_event(
                "streaming-job", {"type": "step", "step": 4, "label": "인젝션 탐지 중..."}
            )
            second = json.loads(
                (await asyncio.wait_for(anext(gen), timeout=2.0))[len("data: ") :]
            )
            return first, second
        finally:
            await gen.aclose()

    first, second = asyncio.run(scenario())

    assert first["type"] == "snapshot"
    assert [a["jobId"] for a in first["active"]] == ["streaming-job"]
    assert second["type"] == "activity"
    assert second["stage"] == "injection"
