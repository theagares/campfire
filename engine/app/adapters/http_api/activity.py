"""
app/adapters/http_api/activity.py
GET /activity        — 지금 처리 중인 job 스냅샷 (폴링용)
GET /activity/stream — 처리 단계 변화를 실시간으로 흘려보내는 SSE

/jobs/{id}/events 와 다른 점: 그쪽은 job id 를 아는 요청자가 자기 job 의 기록을
재생하는 용도다. 이쪽은 job id 를 모르는 관찰자(대시보드 처리현황)가 "지금 무슨
단계가 돌고 있는지" 를 보기 위한 단일 방송 채널이다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.core import activity as activity_bus

router = APIRouter()

# 프록시·로드밸런서가 유휴 연결을 끊지 않게 주기적으로 주석 프레임을 보낸다.
_KEEPALIVE_SEC = 15.0


@router.get("/activity")
async def get_activity() -> dict:
    active = activity_bus.snapshot()
    return {"active": active, "busy": bool(active)}


@router.get("/activity/stream")
async def stream_activity() -> StreamingResponse:
    return StreamingResponse(
        _sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx 등이 SSE 를 버퍼링해 실시간성을 죽이는 것을 막는다.
            "X-Accel-Buffering": "no",
        },
    )


async def _sse():
    queue = activity_bus.subscribe()
    try:
        # 첫 프레임은 현재 상태 — 처리 도중에 접속한 구독자도 즉시 맞는 화면을 그린다.
        yield _frame({"type": "snapshot", "active": activity_bus.snapshot()})
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SEC)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield _frame(event)
    except asyncio.CancelledError:
        # 클라이언트가 끊었다 — 조용히 정리한다.
        raise
    finally:
        with contextlib.suppress(Exception):
            activity_bus.unsubscribe(queue)


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
