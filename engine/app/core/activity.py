"""
app/core/activity.py
파이프라인 처리 활동을 실시간으로 방송한다 (대시보드 "처리현황" 동기화용).

왜 필요한가: job 이벤트는 job_registry 에 기록되지만, /jobs 는 요청 안에서 동기로
처리를 끝내기 때문에 /jobs/{id}/events 는 "이미 끝난 일"을 재생할 수만 있다. 게다가
그 job id 를 아는 쪽(확장 프로그램·MCP)만 볼 수 있어서, 대시보드처럼 "지금 뭔가
처리 중인가"를 알고 싶은 관찰자는 볼 방법이 아예 없었다(그래서 처리현황 화면이
idle 로 고정돼 있었다).

이 모듈은 job id 를 모르는 관찰자도 구독할 수 있는 단일 방송 채널이다:
    구독자(SSE) ← publish() ← job_event()/job_started()/job_finished()

설계 메모:
- 구독자 큐는 유한(_QUEUE_MAX)하다. 느린 구독자가 밀리면 그 구독자의 가장 오래된
  이벤트를 버린다 — 방송이 producer(실제 탐지 파이프라인)를 절대 막지 않게 하기
  위해서다. 처리현황은 "지금 상태"가 중요하지 과거 이벤트 보존이 중요한 화면이 아니다.
- 늦게 접속한 구독자를 위해 진행 중인 job 상태를 snapshot() 으로 준다. 이게 없으면
  처리 도중에 대시보드를 켠 사용자는 job 이 끝날 때까지 아무것도 못 본다.
- 이벤트 유실이 곧 "영원히 탐지중" 으로 굳는 것을 막으려고 _active 항목에는
  started_at 을 둔다. 소비자는 이 값으로 오래된 항목을 스스로 정리할 수 있다.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

# 구독자 하나가 밀렸을 때 들고 있을 최대 이벤트 수.
_QUEUE_MAX = 64

_subscribers: set[asyncio.Queue] = set()

# job_id -> {"jobId","stage","label","source","startedAt"}
_active: dict[str, dict[str, Any]] = {}

# orchestrator 가 내보내는 step 번호 → 처리현황이 아는 단계 이름.
# (step 3 은 없다 — 청크 분할이 step 2 에 합쳐져 있다. orchestrator 참고)
_STEP_STAGE = {
    1: "parse",
    2: "pii",
    4: "injection",
    5: "mask",
}


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def snapshot() -> list[dict[str, Any]]:
    """지금 처리 중인 job 목록. 늦게 접속한 구독자에게 첫 프레임으로 준다."""
    return list(_active.values())


def _publish(event: dict[str, Any]) -> None:
    """모든 구독자에게 흘려보낸다. 큐가 찬 구독자는 가장 오래된 것을 버리고 넣는다
    (producer 를 막지 않는다 — 이 함수는 탐지 경로 안에서 불린다)."""
    for q in list(_subscribers):
        while True:
            try:
                q.put_nowait(event)
                break
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()  # 가장 오래된 것 폐기 후 재시도
            except Exception:  # noqa: BLE001 - 방송 실패가 탐지를 막으면 안 된다
                break


def job_started(job_id: str, source: str = "job") -> None:
    _active[job_id] = {
        "jobId": job_id,
        "stage": "receive",
        "label": "요청 접수",
        "source": source,
        "startedAt": time.time(),
    }
    _publish({"type": "activity", "phase": "start", **_active[job_id]})


def job_event(job_id: str, event: dict[str, Any]) -> None:
    """orchestrator 의 step/warning 이벤트를 단계 변화로 바꿔 방송한다."""
    etype = event.get("type")
    if etype == "step":
        stage = _STEP_STAGE.get(event.get("step"))
        if stage is None:
            return
        entry = _active.get(job_id)
        if entry is None:
            job_started(job_id)
            entry = _active[job_id]
        entry["stage"] = stage
        entry["label"] = event.get("label") or stage
        _publish(
            {
                "type": "activity",
                "phase": "end" if event.get("done") else "progress",
                **entry,
            }
        )
    elif etype in ("done", "error"):
        job_finished(job_id, ok=(etype == "done"), error=event.get("message"))


def job_finished(job_id: str, ok: bool = True, error: str | None = None) -> None:
    entry = _active.pop(job_id, None)
    _publish(
        {
            "type": "activity",
            "phase": "finish",
            "jobId": job_id,
            "stage": "done" if ok else "error",
            "label": "처리 완료" if ok else (error or "처리 실패"),
            "source": (entry or {}).get("source", "job"),
            "startedAt": (entry or {}).get("startedAt"),
            "ok": ok,
        }
    )


def reset() -> None:
    """테스트용 — 구독자/진행 상태를 비운다."""
    _subscribers.clear()
    _active.clear()
