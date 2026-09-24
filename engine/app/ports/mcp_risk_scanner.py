"""Stable application contract for the optional MCP risk-scanner sidecar."""

from __future__ import annotations

from typing import Any, Protocol


class RiskScannerUnavailable(RuntimeError):
    """The optional scanner is disabled, failed to start, or disconnected."""


class RiskScannerRejected(ValueError):
    """The scanner rejected an invalid or out-of-budget snapshot."""


class McpRiskScanner(Protocol):
    """Narrow port: the app does not depend on scanner implementation modules."""

    def status(self) -> dict[str, Any]: ...

    async def assess_snapshot(
        self,
        *,
        server_id: str,
        tools: list[dict[str, Any]],
        scopes: list[str] | None = None,
        baseline: dict[str, Any] | None = None,
        runtime_events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...
