"""Standalone stdio MCP gateway for explicitly approved loopback servers.

This is a message-level guard. It cannot see the upstream process's private file,
network, or environment access; OS-level runtime monitoring is out of scope.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .probe import UnsafeTargetError, call_checked, list_checked, validate_loopback_url

_REASON_CODES = {
    "sensitive or dangerous tool arguments blocked": "sensitive_argument",
    "poisoned or sensitive tool result blocked": "poisoned_result",
    "tool definitions changed since explicit approval": "catalog_changed",
    "critical tool definition finding": "critical_definition",
    "unapproved tool": "unapproved_tool",
    "uninspectable non-text tool result blocked": "uninspectable_result",
    "tool result exceeds inspection limit": "uninspectable_result",
}


def create_server(url: str, baseline: dict[str, Any], audit_file: Path | None = None) -> Server:
    validate_loopback_url(url)
    if baseline.get("serverId") != url or baseline.get("format") != 1:
        raise ValueError("baseline does not match upstream")
    server = Server("campfire-risk-proxy")
    audit_lock = asyncio.Lock()

    async def audit(name: str, decision: str, reason_code: str | None = None) -> None:
        if audit_file is None:
            return
        event = {"timestamp": time.time(), "tool": name[:128], "decision": decision}
        if reason_code:
            event["reasonCode"] = reason_code
        # The audit never contains arguments, results, credentials, or raw descriptions.
        async with audit_lock:
            with audit_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, separators=(",", ":")) + "\n")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return await list_checked(url, baseline)

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            result = await call_checked(url, baseline, name, arguments)
            await audit(name, "allowed")
            return result
        except UnsafeTargetError as exc:
            await audit(name, "blocked", _REASON_CODES.get(str(exc), "uninspectable_result"))
            return types.CallToolResult(isError=True, content=[
                types.TextContent(type="text", text="Campfire policy blocked the MCP call")
            ])

    return server


async def run_stdio_proxy(url: str, baseline_file: Path, audit_file: Path | None = None) -> None:
    baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    server = create_server(url, baseline, audit_file)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
