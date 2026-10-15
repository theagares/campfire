"""
app/core/demo_trace.py
시연 전용 기밀 판정 추적 — SECUREDOC_DEMO_TRACE=1 일 때만 동작 (기본 꺼짐).

기밀 탐지가 단계마다 남기는 것(KG 후보 → 리트리버 점수 → Solar 답 → 가린 문장)을 모아
/demo/trace/stream 으로 흘려보낸다. "어느 KG 사실 때문에 이 문장이 가려졌나" 는 탐지 함수 안에만
있다가 버려지므로(Detection 에는 사실이 없다) 여기서 따로 붙잡는다.

원문 비저장 원칙의 예외라 범위를 좁게 둔다:
- 디스크에 쓰지 않는다. 메모리에 최근 _MAX_SCANS 건만, 엔진을 끄면 사라진다
- 문장은 Solar 에 보낸 그대로(PII·사용자 지정 단어·인젝션 구간을 가린 것)만 싣는다
- 처리현황 방송(activity.py)은 문서 내용을 못 싣게 일부러 만든 통로라 쓰지 않고 따로 둔다
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections import OrderedDict
from typing import Any

from app import config

_MAX_SCANS = 20
_QUEUE_MAX = 256

_scans: OrderedDict[int, list[dict[str, Any]]] = OrderedDict()  # 검사 번호 → 이벤트 목록 (최근 _MAX_SCANS 건)
_subscribers: set[asyncio.Queue] = set()
_ids = itertools.count(1)


def start() -> int | None:
    """새 검사의 추적 번호. 꺼져 있으면 None — publish 도 아무것도 안 한다."""
    if not config.DEMO_TRACE_ENABLED:
        return None
    sid = next(_ids)
    _scans[sid] = []
    while len(_scans) > _MAX_SCANS:
        _scans.popitem(last=False)
    return sid


def publish(scan: int | None, stage: str, **data: Any) -> None:
    if scan is None:
        return
    event = {"type": "trace", "scan": scan, "stage": stage, "at": time.time(), **data}
    if scan in _scans:
        _scans[scan].append(event)
    for q in list(_subscribers):  # 느린 구독자는 가장 오래된 것을 버린다 — 탐지를 막지 않는다
        while True:
            try:
                q.put_nowait(event)
                break
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            except Exception:  # noqa: BLE001 - 방송 실패가 탐지를 막으면 안 된다
                break


def snapshot() -> list[dict[str, Any]]:
    """남아 있는 검사들의 이벤트 전부 (오래된 순) — 늦게 접속한 화면이 지난 검사부터 그린다."""
    return [e for events in _scans.values() for e in events]


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def reset() -> None:
    """테스트용."""
    _scans.clear()
    _subscribers.clear()
