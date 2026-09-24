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

from mcp import ClientSession, types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .core import MAX_AUDIT_BYTES, MAX_BASELINE_BYTES, load_integrity_key, verify_baseline
from .probe import call_observed, list_observed, loopback_session, validate_loopback_url


def create_server(url: str, baseline: dict[str, Any] | None = None,
                  audit_file: Path | None = None,
                  baseline_key: bytes | None = None,
                  upstream_session: ClientSession | None = None) -> Server:
    validate_loopback_url(url)
    if baseline is not None:
        verify_baseline(baseline, server_id=url, integrity_key=baseline_key,
                        require_integrity=baseline_key is not None)
    server = Server("campfire-risk-observer")
    audit_lock = asyncio.Lock()

    async def audit(name: str, decision: str, signals: list[str]) -> None:
        if audit_file is None:
            return
        event = {"timestamp": time.time(), "tool": name[:128], "decision": decision,
                 "signals": signals}
        encoded = (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
        # The audit never contains arguments, results, credentials, or raw descriptions.
        async with audit_lock:
            try:
                current_size = audit_file.stat().st_size if audit_file.exists() else 0
                if current_size + len(encoded) > MAX_AUDIT_BYTES:
                    raise OSError("audit size limit reached")
                with audit_file.open("ab") as stream:
                    stream.write(encoded)
            except OSError:
                # Monitoring failure must not turn an otherwise valid target call
                # into a policy block; tell the operator coverage is incomplete.
                print("[mcp-risk-observer] audit write failed; runtime coverage is incomplete", file=sys.stderr)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        tools, signals = await list_observed(url, baseline, baseline_key, upstream_session)
        await audit("*", "listed", signals)
        return tools

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        result, signals = await call_observed(url, name, arguments, baseline, baseline_key,
                                              upstream_session)
        await audit(name, "forwarded", signals)
        return result

    return server


async def run_stdio_proxy(url: str, baseline_file: Path | None = None,
                          audit_file: Path | None = None,
                          baseline_key_file: Path | None = None) -> None:
    if baseline_file is not None and baseline_file.stat().st_size > MAX_BASELINE_BYTES:
        raise ValueError(f"baseline exceeds {MAX_BASELINE_BYTES} bytes")
    baseline = json.loads(baseline_file.read_text(encoding="utf-8")) if baseline_file else None
    baseline_key = load_integrity_key(baseline_key_file) if baseline_key_file else None
    if baseline_key is not None and baseline is None:
        raise ValueError("--baseline-key-file requires --baseline")
    async with loopback_session(url) as upstream_session:
        server = create_server(url, baseline, audit_file, baseline_key, upstream_session)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
