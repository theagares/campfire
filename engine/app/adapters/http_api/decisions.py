"""
app/adapters/http_api/decisions.py
프록시가 붙들고 있는 요청에 사람이 답하는 자리.

확장 경로의 사이드패널이 하던 일을 REST 로 낸 것이다. 데스크탑 앱이 이 엔드포인트를
물면 검토 UI 가 되고, 그 전까지는 curl 로도 굴릴 수 있다.

프록시 애드온과 같은 프로세스라 `broker` 객체를 그대로 공유한다.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.adapters.proxy.decision import broker

router = APIRouter()


class DecisionBody(BaseModel):
    # send_masked: 마스킹본을 보낸다 (기본)
    # send_original: 원본 그대로 보낸다
    # cancel: 보내지 않는다
    action: str


@router.get("/decisions")
async def list_decisions() -> dict:
    """지금 사람을 기다리고 있는 건들. 원문은 들어가지 않는다."""
    return {"pending": broker.list_pending()}


@router.get("/decisions/{decision_id}")
async def get_decision(decision_id: str) -> dict:
    """원문/마스킹본까지 포함한 상세 — 검토 화면이 diff 를 그리는 데 쓴다."""
    detail = broker.detail(decision_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="이미 끝났거나 없는 건입니다")
    return detail


@router.post("/decisions/{decision_id}")
async def resolve_decision(decision_id: str, body: DecisionBody) -> dict:
    if body.action not in ("send_masked", "send_original", "cancel"):
        raise HTTPException(status_code=400, detail=f"모르는 action: {body.action}")
    # 409 로 구분하는 이유: 사용자가 두 번 눌렀을 때와 없는 id 를 부른 것을 UI 가
    # 다르게 보여줄 수 있어야 한다. 타임아웃으로 사라진 것도 여기로 온다.
    if not broker.resolve(decision_id, body.action):  # type: ignore[arg-type]
        raise HTTPException(status_code=409, detail="이미 처리됐거나 시간이 지난 건입니다")
    return {"ok": True, "id": decision_id, "action": body.action}
