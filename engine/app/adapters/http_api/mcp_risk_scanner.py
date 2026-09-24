"""Versioned local HTTP boundary for the optional MCP risk-scanner sidecar."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.mcp_risk_scanner import CONTRACT_VERSION
from app.ports.mcp_risk_scanner import (
    McpRiskScanner,
    RiskScannerRejected,
    RiskScannerUnavailable,
)

router = APIRouter(prefix="/mcp-risk-scanner/v1", tags=["mcp-risk-scanner"])


class AssessSnapshotRequest(BaseModel):
    """Bound the collection counts here; the sidecar enforces encoded byte budgets."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    server_id: str = Field(alias="serverId", min_length=1, max_length=256)
    tools: list[dict[str, Any]] = Field(max_length=200)
    scopes: list[str] | None = Field(default=None, max_length=100)
    baseline: dict[str, Any] | None = None
    runtime_events: list[dict[str, Any]] | None = Field(
        default=None, alias="runtimeEvents", max_length=10_000
    )


def _scanner(request: Request) -> McpRiskScanner:
    scanner = getattr(request.app.state, "mcp_risk_scanner", None)
    if scanner is None:
        raise HTTPException(status_code=503, detail="MCP risk scanner is unavailable")
    return scanner


@router.get("/status")
async def scanner_status(request: Request):
    scanner = getattr(request.app.state, "mcp_risk_scanner", None)
    if scanner is None:
        return {
            "contractVersion": CONTRACT_VERSION,
            "enabled": False,
            "state": "unavailable",
            "transport": "stdio-mcp",
            "blocksTargetUse": False,
            "scoreAffectsAppBehavior": False,
        }
    return scanner.status()


@router.post("/assess")
async def assess_snapshot(body: AssessSnapshotRequest, request: Request):
    scanner = _scanner(request)
    try:
        report = await scanner.assess_snapshot(
            server_id=body.server_id,
            tools=body.tools,
            scopes=body.scopes,
            baseline=body.baseline,
            runtime_events=body.runtime_events,
        )
    except RiskScannerRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RiskScannerUnavailable as exc:
        raise HTTPException(status_code=503, detail="MCP risk scanner is unavailable") from exc
    return {"contractVersion": CONTRACT_VERSION, "available": True, "report": report}
