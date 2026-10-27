"""
app/adapters/http_api/job_registry.py
인메모리 job 이벤트 저장 (PLAN §9.1: 원문은 인메모리·세션 한정, 디스크 미저장).

룰베이스 v1 은 요청 내에서 동기적으로 파이프라인을 끝내므로, 이벤트를 순서대로
기록해두고 /jobs/{id}/events 가 폴링/SSE 로 재생한다.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import Any

from app.core import activity as activity_bus

# job_id -> {"events": [...], "done": bool, "event_bytes": int, ...}
_jobs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_MAX_JOBS = 200
_MAX_EVENTS_PER_JOB = 64
_MAX_EVENT_BYTES_PER_JOB = 64 * 1024
_COMPLETED_TTL_SECONDS = 60.0


def _event_size(event: dict) -> int:
    """이벤트 보관량의 안정적인 상한을 위한 UTF-8 JSON 크기."""
    return len(json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _prune(now: float | None = None) -> None:
    """완료된 job은 짧게 재생한 뒤 버리고, 전체 job 수도 제한한다."""
    current = time.time() if now is None else now
    expired = [
        job_id for job_id, job in _jobs.items()
        if job.get("completed_at") is not None
        and current - float(job["completed_at"]) >= _COMPLETED_TTL_SECONDS
    ]
    for job_id in expired:
        _jobs.pop(job_id, None)
    while len(_jobs) > _MAX_JOBS:
        _jobs.popitem(last=False)


def create_job(job_id: str) -> None:
    _prune()
    _jobs[job_id] = {
        "events": [], "done": False, "created_at": time.time(),
        "completed_at": None, "event_bytes": 0, "next_seq": 1,
    }
    _prune()


def record_event(job_id: str, event: dict) -> dict:
    _prune()
    job = _jobs.get(job_id)
    if job is None:
        create_job(job_id)
        job = _jobs[job_id]
    ev = dict(event)
    ev["seq"] = job["next_seq"]
    job["next_seq"] += 1
    ev_size = _event_size(ev)
    if ev_size > _MAX_EVENT_BYTES_PER_JOB:
        # 단일 이벤트 자체가 상한보다 크면 앞 이벤트를 아무리 지워도 제한을 못 지킨다.
        # 진행 UI에 필요한 최소 메타만 남기고 큰 result/임의 payload는 버린다.
        compact = {"type": ev.get("type", "event"), "seq": ev["seq"], "truncated": True}
        for key in ("message", "label", "asset"):
            if ev.get(key) is not None:
                compact[key] = str(ev[key])[: max(16, _MAX_EVENT_BYTES_PER_JOB // 4)]
        if _event_size(compact) > _MAX_EVENT_BYTES_PER_JOB:
            compact = {"type": ev.get("type", "event"), "seq": ev["seq"], "truncated": True}
        ev = compact
        ev_size = _event_size(ev)
    job["events"].append(ev)
    job["event_bytes"] += ev_size

    # 진행 이벤트가 많은 작업도 보관량이 문서 크기와 함께 커지지 않게 한다. seq는
    # 별도 카운터라 앞 이벤트를 버려도 after=N 폴링 계약이 깨지지 않는다.
    while len(job["events"]) > 1 and (
        len(job["events"]) > _MAX_EVENTS_PER_JOB
        or job["event_bytes"] > _MAX_EVENT_BYTES_PER_JOB
    ):
        dropped = job["events"].pop(0)
        job["event_bytes"] -= _event_size(dropped)
    if ev.get("type") in ("done", "error"):
        job["done"] = True
        job["completed_at"] = time.time()
    return ev


def has_job(job_id: str) -> bool:
    _prune()
    return job_id in _jobs


def get_events(job_id: str, after: int = 0) -> list[dict]:
    _prune()
    job = _jobs.get(job_id)
    if job is None:
        return []
    return [e for e in job["events"] if e.get("seq", 0) > after]


def is_done(job_id: str) -> bool:
    _prune()
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
