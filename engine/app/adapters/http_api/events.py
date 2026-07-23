"""
app/adapters/http_api/events.py
GET /jobs/{id}/events — 진행 이벤트.

두 가지 소비 방식 지원:
  - 기본(폴링, EC2 계약 호환): ?after=N → {"events":[...], "done":bool}
  - SSE: Accept: text/event-stream 또는 ?stream=1 → text/event-stream
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from . import job_registry

router = APIRouter()


@router.get("/jobs/{job_id}/events")
async def poll_job_events(
    request: Request,
    job_id: str,
    after: int = Query(0, ge=0),
    stream: int = Query(0),
):
    if not job_registry.has_job(job_id):
        raise HTTPException(status_code=404, detail="job not found")

    accept = request.headers.get("accept", "")
    if stream or "text/event-stream" in accept:
        return StreamingResponse(_sse(job_id, after), media_type="text/event-stream")

    events = job_registry.get_events(job_id, after=after)
    return {"events": events, "done": job_registry.is_done(job_id)}


async def _sse(job_id: str, after: int):
    """이미 기록된 이벤트를 순서대로 흘려보내고 종료(v1 은 동기 처리라 즉시 완료)."""
    for ev in job_registry.get_events(job_id, after=after):
        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    yield 'data: {"type":"eof"}\n\n'
