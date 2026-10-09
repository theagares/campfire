"""Authenticated localhost control plane for mandatory mask terms."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app import config
from app.core import forced_mask

router = APIRouter(prefix="/internal/forced-mask-rules", tags=["internal"])


class RulesPayload(BaseModel):
    terms: list[str]


def _authorize(authorization: str | None) -> None:
    expected = config.INTERNAL_CONTROL_TOKEN
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    # A build not launched by the desktop has no control endpoint.  Return 404
    # for both cases so an unauthenticated local process cannot probe it.
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=404, detail="not found")


@router.put("")
async def replace_rules(
    payload: RulesPayload,
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    try:
        forced_mask.registry.configure(payload.terms)
    except forced_mask.ForcedMaskRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return forced_mask.registry.status()
