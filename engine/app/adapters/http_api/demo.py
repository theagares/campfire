"""
app/adapters/http_api/demo.py
시연 전용 — SECUREDOC_DEMO_TRACE=1 일 때만 응답하고, 아니면 404.
  GET /demo/dashboard     실시간 KG 기밀 판정 대시보드 (HTML 한 파일)
  GET /demo/kg            설정된 KG 사실 전부 (그래프 배경)
  GET /demo/trace/stream  기밀 판정 과정 SSE — 첫 프레임은 메모리에 남은 지난 검사들

페이지를 엔진이 직접 줘서(같은 출처) CORS 는 넓히지 않는다 — 엔진 CORS 는 확장 프로그램만 허용한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from app import config
from app.core import demo_trace
from app.core.detectors.confidential import kg

router = APIRouter(prefix="/demo")

_PAGE = Path(__file__).with_name("demo_dashboard.html")
_KEEPALIVE_SEC = 15.0


def _require_enabled() -> None:
    if not config.DEMO_TRACE_ENABLED:
        raise HTTPException(status_code=404)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    _require_enabled()
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@router.get("/kg")
async def kg_triples() -> dict:
    _require_enabled()
    graph = await asyncio.to_thread(kg.get, config.KG_PATH)
    return {
        "triples": [[t.s, t.p, t.o] for t in graph.triples] if graph else [],
        "delta": config.CONFIDENTIAL_DELTA,
        "confidentialEnabled": config.CONFIDENTIAL_ENABLED,
    }


@router.get("/trace/stream")
async def trace_stream() -> StreamingResponse:
    _require_enabled()
    return StreamingResponse(_sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _sse():
    queue = demo_trace.subscribe()  # 스냅샷보다 먼저 구독 — 사이에 온 이벤트는 화면이 (검사, 단계) 로 중복을 거른다
    try:
        yield _frame({"type": "snapshot", "events": demo_trace.snapshot()})
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SEC)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield _frame(event)
    finally:
        with contextlib.suppress(Exception):
            demo_trace.unsubscribe(queue)


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
