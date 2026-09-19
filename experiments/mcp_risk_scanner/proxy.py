"""Standalone observation-only stdio MCP relay for chosen loopback servers.

This is a message-level observer, not an enforcement boundary. It cannot see the
upstream process's private file, network, or environment access.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .probe import call_observed, list_observed, validate_loopback_url


def create_server(url: str, baseline: dict[str, Any] | None = None,
                  audit_file: Path | None = None) -> Server:
    validate_loopback_url(url)
    if baseline is not None and (baseline.get("serverId") != url or baseline.get("format") != 1):
        raise ValueError("baseline does not match upstream")
    server = Server("campfire-risk-observer")
    audit_lock = asyncio.Lock()

    async def audit(name: str, decision: str, signals: list[str]) -> None:
        if audit_file is None:
            return
        event = {"timestamp": time.time(), "tool": name[:128], "decision": decision,
                 "signals": signals}
        # The audit never contains arguments, results, credentials, or raw descriptions.
        async with audit_lock:
            try:
                with audit_file.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, separators=(",", ":")) + "\n")
            except OSError:
                # Monitoring failure must not turn an otherwise valid target call
                # into a policy block; tell the operator coverage is incomplete.
                print("[mcp-risk-observer] audit write failed; runtime coverage is incomplete", file=sys.stderr)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        tools, signals = await list_observed(url, baseline)
        await audit("*", "listed", signals)
        return tools

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        result, signals = await call_observed(url, name, arguments, baseline)
        await audit(name, "forwarded", signals)
        return result

    return server


async def run_stdio_proxy(url: str, baseline_file: Path | None = None,
                          audit_file: Path | None = None) -> None:
    baseline = json.loads(baseline_file.read_text(encoding="utf-8")) if baseline_file else None
    server = create_server(url, baseline, audit_file)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
