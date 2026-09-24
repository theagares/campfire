"""Fail-open lifecycle adapter for the separately packaged risk-scanner MCP."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from app import config
from app.ports.mcp_risk_scanner import RiskScannerRejected, RiskScannerUnavailable

logger = logging.getLogger("securedoc.engine.mcp_risk_scanner")

CONTRACT_VERSION = 1
MAX_ADAPTER_REQUEST_BYTES = 2_500_000
MAX_ADAPTER_RESPONSE_BYTES = 2_500_000
REQUIRED_TOOL = "assess_mcp_snapshot"


class StdioMcpRiskScanner:
    """Own one persistent stdio MCP session for the engine lifespan.

    This adapter deliberately exposes only deterministic snapshot assessment. It does
    not start the target MCP, opt into loopback probing, or grant Solar cloud consent.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        python_executable: str,
        module: str,
        pythonpath: Path,
        start_timeout: float,
        call_timeout: float,
    ) -> None:
        self.enabled = enabled
        self.python_executable = python_executable
        self.module = module
        self.pythonpath = pythonpath
        self.start_timeout = max(0.1, start_timeout)
        self.call_timeout = max(0.1, call_timeout)
        self._state = "disabled" if not enabled else "starting"
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._call_lock = asyncio.Lock()

    @classmethod
    def from_config(cls) -> "StdioMcpRiskScanner":
        return cls(
            enabled=config.MCP_RISK_SCANNER_ENABLED,
            python_executable=config.MCP_RISK_SCANNER_PYTHON_EXECUTABLE,
            module=config.MCP_RISK_SCANNER_MODULE,
            pythonpath=config.MCP_RISK_SCANNER_PYTHONPATH,
            start_timeout=config.MCP_RISK_SCANNER_START_TIMEOUT_SEC,
            call_timeout=config.MCP_RISK_SCANNER_CALL_TIMEOUT_SEC,
        )

    def status(self) -> dict[str, Any]:
        return {
            "contractVersion": CONTRACT_VERSION,
            "enabled": self.enabled,
            "state": self._state,
            "transport": "stdio-mcp",
            "blocksTargetUse": False,
            "scoreAffectsAppBehavior": False,
        }

    async def start(self) -> None:
        if not self.enabled:
            self._state = "disabled"
            return
        if self._session is not None:
            return
        self._state = "starting"
        stack = AsyncExitStack()
        try:
            params = StdioServerParameters(
                command=self.python_executable,
                args=["-m", self.module, "serve"],
                cwd=self.pythonpath,
                env={
                    # Do not pass the engine's full environment (which may contain API
                    # keys). Static assessment does not need secrets or network consent.
                    "PYTHONPATH": str(self.pythonpath),
                    "PYTHONUTF8": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            # MCP/AnyIO transports install task-local cancel scopes. Entering their
            # async contexts through asyncio.wait_for() would create another task and
            # make shutdown fail with "exit cancel scope in a different task".
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=self.start_timeout),
                )
            )
            await session.initialize()
            catalog = await session.list_tools()
            if REQUIRED_TOOL not in {tool.name for tool in catalog.tools}:
                raise RuntimeError("scanner MCP contract is missing assess_mcp_snapshot")
            self._stack = stack
            self._session = session
            self._state = "ready"
        except BaseException:
            self._state = "unavailable"
            await stack.aclose()
            raise

    async def close(self) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        if stack is not None:
            await stack.aclose()
        self._state = "disabled" if not self.enabled else "stopped"

    async def assess_snapshot(
        self,
        *,
        server_id: str,
        tools: list[dict[str, Any]],
        scopes: list[str] | None = None,
        baseline: dict[str, Any] | None = None,
        runtime_events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        session = self._session
        if self._state != "ready" or session is None:
            raise RiskScannerUnavailable(f"risk scanner is {self._state}")
        arguments: dict[str, Any] = {"server_id": server_id, "tools": tools}
        if scopes is not None:
            arguments["scopes"] = scopes
        if baseline is not None:
            arguments["baseline"] = baseline
        if runtime_events is not None:
            arguments["runtime_events"] = runtime_events
        try:
            encoded = json.dumps(
                arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
            raise RiskScannerRejected("snapshot is not valid JSON data") from exc
        if len(encoded) > MAX_ADAPTER_REQUEST_BYTES:
            raise RiskScannerRejected("scanner request is too large")

        async with self._call_lock:
            try:
                result = await session.call_tool(
                    REQUIRED_TOOL,
                    arguments,
                    read_timeout_seconds=timedelta(seconds=self.call_timeout),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._state = "unavailable"
                raise RiskScannerUnavailable("risk scanner call failed") from exc
        if result.isError:
            raise RiskScannerRejected("risk scanner rejected the snapshot")
        payload = result.structuredContent
        if payload is None:
            text = next(
                (str(item.text) for item in result.content if hasattr(item, "text")), None
            )
            if text is None:
                self._state = "unavailable"
                raise RiskScannerUnavailable("risk scanner returned no JSON report")
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, RecursionError) as exc:
                self._state = "unavailable"
                raise RiskScannerUnavailable("risk scanner returned invalid JSON") from exc
        if not isinstance(payload, dict):
            self._state = "unavailable"
            raise RiskScannerUnavailable("risk scanner returned an invalid report")
        try:
            response_size = len(json.dumps(payload, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
            self._state = "unavailable"
            raise RiskScannerUnavailable("risk scanner returned an invalid report") from exc
        if response_size > MAX_ADAPTER_RESPONSE_BYTES:
            self._state = "unavailable"
            raise RiskScannerUnavailable("risk scanner report is too large")
        required = {"serverId", "riskScore", "securityScore", "coverage", "findings"}
        if not required.issubset(payload):
            self._state = "unavailable"
            raise RiskScannerUnavailable("risk scanner report contract is incomplete")
        risk = payload["riskScore"]
        security = payload["securityScore"]
        valid_scores = (
            isinstance(risk, int)
            and not isinstance(risk, bool)
            and isinstance(security, int)
            and not isinstance(security, bool)
            and 0 <= risk <= 100
            and security == 100 - risk
        )
        if (
            payload["serverId"] != server_id
            or not valid_scores
            or not isinstance(payload["coverage"], dict)
            or not isinstance(payload["findings"], list)
        ):
            self._state = "unavailable"
            raise RiskScannerUnavailable("risk scanner report contract is invalid")
        return payload


@asynccontextmanager
async def risk_scanner_context() -> AsyncIterator[StdioMcpRiskScanner]:
    """Start optionally and degrade without preventing the Campfire engine startup."""
    scanner = StdioMcpRiskScanner.from_config()
    if scanner.enabled:
        try:
            await scanner.start()
        except Exception:
            logger.exception("optional MCP risk scanner failed to start")
    try:
        yield scanner
    finally:
        try:
            await scanner.close()
        except Exception:
            logger.exception("optional MCP risk scanner failed to stop cleanly")
