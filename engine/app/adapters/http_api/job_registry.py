"""
app/adapters/http_api/job_registry.py
인메모리 job 이벤트 저장 (PLAN §9.1: 원문은 인메모리·세션 한정, 디스크 미저장).

룰베이스 v1 은 요청 내에서 동기적으로 파이프라인을 끝내므로, 이벤트를 순서대로
기록해두고 /jobs/{id}/events 가 폴링/SSE 로 재생한다.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from app.core import activity as activity_bus

# job_id -> {"events": [...], "done": bool}
_jobs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_MAX_JOBS = 200  # 메모리 상한 (오래된 것부터 폐기)


def create_job(job_id: str) -> None:
    _jobs[job_id] = {"events": [], "done": False, "created_at": time.time()}
    while len(_jobs) > _MAX_JOBS:
        _jobs.popitem(last=False)


def record_event(job_id: str, event: dict) -> dict:
    job = _jobs.get(job_id)
    if job is None:
        create_job(job_id)
        job = _jobs[job_id]
    ev = dict(event)
    ev["seq"] = len(job["events"]) + 1
    job["events"].append(ev)
    if ev.get("type") in ("done", "error"):
        job["done"] = True
    return ev


def has_job(job_id: str) -> bool:
    return job_id in _jobs


def get_events(job_id: str, after: int = 0) -> list[dict]:
    job = _jobs.get(job_id)
    if job is None:
        return []
    return [e for e in job["events"] if e.get("seq", 0) > after]


def is_done(job_id: str) -> bool:
    job = _jobs.get(job_id)
    return bool(job and job["done"])


def make_emit(job_id: str, *, activity: bool = False):
    """job 이벤트 기록용 emit 을 만든다.

    activity=True 면 대시보드 "처리현황" 방송(core.activity)에도 같이 흘린다.
    모델 다운로드(/models/fetch)도 같은 job 이벤트 구조를 쓰지만 그건 탐지
    파이프라인이 아니므로, 방송은 실제 파이프라인 job 만 옵트인한다 — 안 그러면
    다운로드 진행률이 처리현황을 "탐지중" 으로 만든다.
    """

    async def emit(event: dict) -> None:
        record_event(job_id, event)
        if activity:
            activity_bus.job_event(job_id, event)

    return emit
