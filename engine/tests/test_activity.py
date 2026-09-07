"""처리현황 실시간 방송(core.activity + /activity*) 테스트.

대시보드가 "지금 무슨 단계가 돌고 있는지" 를 볼 수 있어야 한다는 계약을 지킨다.
"""

import asyncio
import json

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
    # 오래된 것이 버려지고 최신이 남는다. 방송 id 는 불투명하므로(진짜 job id 를
    # 흘리지 않는다) 내부 매핑으로 대조한다.
    assert drained[-1]["jobId"] == activity_bus._active["job-499"]["jobId"]


def test_snapshot_lets_late_subscriber_catch_up():
    """처리 도중 접속한 구독자도 현재 진행 상태를 알 수 있어야 한다."""
    activity_bus.job_started("in-flight", source="extension")
    activity_bus.job_event("in-flight", {"type": "step", "step": 2, "label": "PII 탐지 중..."})

    snap = activity_bus.snapshot()
    assert len(snap) == 1
    # 방송에는 진짜 job id 가 실리지 않는다 — 실리면 그 id 로 /jobs/{id}/events 를 쳐서
    # 원문(originalText)을 그대로 받아갈 수 있다.
    assert snap[0]["jobId"] and snap[0]["jobId"] != "in-flight"
    assert snap[0]["stage"] == "pii"
    assert snap[0]["source"] == "extension"

    activity_bus.job_finished("in-flight", ok=True)
    assert activity_bus.snapshot() == []


def test_failed_job_clears_busy_state():
    """실패한 job 이 방송에서 안 끝나면 대시보드가 영원히 '탐지중' 으로 남는다."""
    activity_bus.job_started("boom")
    activity_bus.job_event("boom", {"type": "error", "message": "파싱 실패"})
    assert activity_bus.snapshot() == []


# ── MCP 경로 방송 ────────────────────────────────────────────────────────────
# HTTP(확장) 경로만 방송하고 MCP 경로는 activity 를 아예 부르지 않아서, AI 클라이언트로
# 검사를 돌리면 처리현황이 계속 idle 이었다. 실제 모델 없이도 배선을 검증할 수 있게
# run_pipeline 을 가짜로 바꿔 단계 이벤트만 흘려보낸다.
def _fake_pipeline(*, steps=(1, 2, 4, 5), fail=False):
    async def fake(**kwargs):
        emit = kwargs.get("emit")
        for step in steps:
            if emit is not None:
                await emit({"type": "step", "step": step, "label": f"step {step}"})
        if fail:
            raise RuntimeError("파싱 실패")
        return {"maskedText": "", "piiItems": [], "injectionItems": [], "stats": {}}

    return fake


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_mcp_scan_broadcasts_stages(monkeypatch):
    """MCP 도구로 검사해도 처리현황에 단계가 떠야 한다."""
    from app.adapters.mcp import tools

    monkeypatch.setattr(tools, "run_pipeline", _fake_pipeline())
    monkeypatch.setattr(tools, "_record", lambda *a, **k: None)

    q = activity_bus.subscribe()
    asyncio.run(tools.scan_text("홍길동 010-1234-5678"))
    events = _drain(q)
    activity_bus.unsubscribe(q)

    stages = [e["stage"] for e in events]
    assert events[0]["phase"] == "start"
    assert events[0]["source"] == "mcp", "MCP 에서 온 것으로 표시돼야 한다"
    assert "pii" in stages, "PII 탐지 단계('탐지 중')가 방송돼야 한다"
    assert "injection" in stages
    assert events[-1]["phase"] == "finish" and events[-1]["ok"] is True
    assert activity_bus.snapshot() == []


def test_mcp_scan_failure_still_clears_busy(monkeypatch):
    """도구가 예외로 끝나도 처리현황이 '탐지중' 으로 굳으면 안 된다."""
    from app.adapters.mcp import tools

    monkeypatch.setattr(tools, "run_pipeline", _fake_pipeline(fail=True))
    monkeypatch.setattr(tools, "_record", lambda *a, **k: None)

    q = activity_bus.subscribe()
    with pytest.raises(RuntimeError):
        asyncio.run(tools.scan_text("boom"))
    events = _drain(q)
    activity_bus.unsubscribe(q)

    assert events[-1]["phase"] == "finish" and events[-1]["ok"] is False
    assert activity_bus.snapshot() == []


def test_mcp_search_does_not_flood_activity(monkeypatch, tmp_path):
    """secure_search_files 는 매칭 라인마다 파이프라인을 돈다 — 방송하면 검색 한 번에
    처리현황이 수백 번 깜빡인다. 여기만은 일부러 조용해야 한다."""
    from app.adapters.mcp import tools

    monkeypatch.setattr(tools, "run_pipeline", _fake_pipeline())
    # 파일 도구는 작업 루트 밖을 거부한다(_resolve). tmp_path 를 루트로 삼지 않으면
    # PathOutsideRootError 로 죽어서 이 테스트가 검증하려는 지점까지 가지도 못한다.
    monkeypatch.setattr(tools, "_PROJECT_ROOT", tmp_path.resolve())
    target = tmp_path / "hits.txt"
    target.write_text("\n".join(f"needle {i}" for i in range(20)), encoding="utf-8")

    q = activity_bus.subscribe()
    asyncio.run(tools.secure_search_files(str(tmp_path), "needle"))
    events = _drain(q)
    activity_bus.unsubscribe(q)

    assert events == [], "검색 스니펫 마스킹은 처리현황에 방송되면 안 된다"
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
    assert len(first["active"]) == 1
    assert first["active"][0]["jobId"] != "streaming-job"
    assert second["type"] == "activity"
    assert second["stage"] == "injection"


def test_broadcast_never_carries_real_job_id():
    """방송에 진짜 job id 가 실리면 안 된다.

    /activity* 는 설계상 인증이 없다("job id 를 모르는 관찰자"도 봐야 한다). 여기에 진짜
    job id 가 실리면 그걸 주운 쪽이 GET /jobs/{id}/events 로 done 이벤트를 받아갈 수 있고,
    거기엔 originalText(문서 원문)와 항목별 실값이 들어 있다 — 원문을 외부에 안 넘기려고
    만든 제품이 자기 API 로 원문을 내주게 된다. 그 고리를 끊은 것이 이 테스트의 계약이다."""
    real = "11111111-2222-3333-4444-555555555555"
    q = activity_bus.subscribe()
    activity_bus.job_started(real, source="extension")
    activity_bus.job_event(real, {"type": "step", "step": 2, "label": "PII 탐지 중..."})
    activity_bus.job_event(real, {"type": "done"})
    frames = _drain(q)

    assert frames, "방송 프레임이 하나도 없다"
    assert real not in json.dumps(frames, ensure_ascii=False), "진짜 job id 가 방송에 샜다"

    # 소비자(대시보드)는 이 값을 Map 키로 쓴다 — 한 job 안에서는 값이 일관돼야
    # 시작한 job 을 마감에서 제대로 지운다.
    ids = {f["jobId"] for f in frames}
    assert len(ids) == 1, f"jobId 가 프레임마다 달라 job 을 추적할 수 없다: {ids}"
    assert frames[0]["phase"] == "start"
    assert frames[-1]["phase"] == "finish"
