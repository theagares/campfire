"""Authenticated localhost control plane for mandatory mask terms."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import config
from app.core import forced_mask

router = APIRouter(prefix="/internal/forced-mask-rules", tags=["internal"])


class RulesPayload(BaseModel):
    terms: list[str]


def _authorize(authorization: str | None) -> None:
    expected = config.INTERNAL_CONTROL_TOKEN
    scheme, _, supplied = (authorization or "").partition(" ")
    # A build not launched by the desktop has no control endpoint.  Return 404
    # for both cases so an unauthenticated local process cannot probe it.
    # Bytes compare: a non-ASCII header would make str compare_digest raise (500).
    if (not expected or scheme != "Bearer"
            or not hmac.compare_digest(expected.encode(), supplied.encode())):
        raise HTTPException(status_code=404, detail="not found")


@router.put("")
async def replace_rules(request: Request):
    # 인증을 본문 검증보다 먼저 한다 — 본문 모델을 인자로 받으면 무인증 요청도 422 를
    # 받아 엔드포인트가 있다는 게 드러난다(404 로 숨긴 의미가 없어진다).
    _authorize(request.headers.get("authorization"))
    try:
        payload = RulesPayload.model_validate(await request.json())
    except ValueError as exc:  # JSON 깨짐·모델 불일치 모두 ValueError 계열
        raise HTTPException(status_code=422, detail="invalid body") from exc
    try:
        forced_mask.registry.configure(payload.terms)
    except forced_mask.ForcedMaskRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return forced_mask.registry.status()
