"""
app/adapters/http_api/proxy_control.py
실행 중에 프록시를 켜고 끈다. 데스크탑 앱의 토글이 부른다.

여기서는 **엔진 쪽 프록시만** 다룬다. 브라우저가 이리로 오게 하는 시스템 프록시
설정은 앱이 만진다 — 엔진은 taskkill /F 로 강제 종료돼 스스로 설정을 되돌릴
기회가 없어서, 되돌리는 책임을 엔진보다 오래 사는 쪽에 둔다.
"""

from __future__ import annotations

import re
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.adapters import proxy

router = APIRouter()

_ROUTE_TTL_SECONDS = 12.0
_MAX_ROUTE_REPORTS = 32
_extension_routes: dict[tuple[str, int, str], dict] = {}


class ExtensionRouteReport(BaseModel):
    """A browser tab's observed protection path (informational, never an allow decision)."""

    mode: Literal["proxy", "extension", "fallback", "off"]
    host: str = Field(min_length=1, max_length=253)
    tabId: int = Field(ge=0)
    expectedProxy: bool


def _prune_extension_routes(now: float) -> None:
    stale = [key for key, value in _extension_routes.items()
             if now - value["seenAt"] > _ROUTE_TTL_SECONDS]
    for key in stale:
        _extension_routes.pop(key, None)


def extension_route_status(now: float | None = None) -> dict:
    """Summarize fresh, actively verified routes reported by extension tabs."""

    current = time.monotonic() if now is None else now
    _prune_extension_routes(current)
    reports = list(_extension_routes.values())
    counts = {"proxy": 0, "extension": 0, "fallback": 0, "off": 0}
    for report in reports:
        counts[report["mode"]] += 1
    active_modes = [mode for mode, count in counts.items() if count]
    mode = active_modes[0] if len(active_modes) == 1 else ("mixed" if active_modes else "unknown")
    latest = max((report["seenAt"] for report in reports), default=None)
    sites = [
        {
            "host": report["host"],
            "mode": report["mode"],
            "expectedProxy": report["expectedProxy"],
        }
        for report in sorted(reports, key=lambda item: (item["host"], item["tabId"]))
    ]
    return {
        "connected": bool(reports),
        "mode": mode,
        "activeTabs": len({report["tabId"] for report in reports}),
        "counts": counts,
        "sites": sites,
        "lastSeenSecondsAgo": max(0.0, current - latest) if latest is not None else None,
    }


def _proxy_status() -> dict:
    return {**proxy.status(), "extensionRoute": extension_route_status()}


@router.get("/proxy/status")
async def proxy_status() -> dict:
    return _proxy_status()


@router.post("/proxy/start")
async def proxy_start() -> dict:
    # 켰는지 여부는 start() 의 반환값이 아니라 status 로 알린다 — 앱은 늘 같은
    # 모양을 받고, "켜라고 했는데 안 켜진" 경우를 running=False 로 알아챈다.
    await proxy.start(force=True)
    return _proxy_status()


@router.post("/proxy/stop")
async def proxy_stop() -> dict:
    await proxy.stop()
    return _proxy_status()


@router.post("/proxy/extension-route")
async def report_extension_route(report: ExtensionRouteReport, request: Request) -> dict:
    # A web page must not be able to paint a reassuring status in the desktop app.
    # CORS blocks reading responses, but it does not reliably prevent blind writes.
    origin = request.headers.get("origin", "")
    if re.fullmatch(r"chrome-extension://[a-p]{32}", origin) is None:
        raise HTTPException(status_code=403, detail="extension origin required")

    now = time.monotonic()
    _prune_extension_routes(now)
    host = report.host.strip().lower().rstrip(".")
    if not host or any(char in host for char in "/:@"):
        raise HTTPException(status_code=422, detail="invalid host")
    key = (origin, report.tabId, host)
    _extension_routes[key] = {
        "mode": report.mode,
        "host": host,
        "tabId": report.tabId,
        "expectedProxy": report.expectedProxy,
        "seenAt": now,
    }
    if len(_extension_routes) > _MAX_ROUTE_REPORTS:
        oldest = min(_extension_routes, key=lambda item: _extension_routes[item]["seenAt"])
        _extension_routes.pop(oldest, None)
    return extension_route_status(now)


@router.get("/proxy/extension-route")
async def get_extension_route() -> dict:
    return extension_route_status()
