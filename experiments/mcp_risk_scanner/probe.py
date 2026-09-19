"""Opt-in loopback MCP metadata probe and observation-only forwarding.

Only numeric loopback addresses are supported in this prototype. Arbitrary remote
URLs would require DNS rebinding/redirect-resistant egress controls.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client

from .core import assess, compare_baseline, inspect_runtime_payload


class UnsafeTargetError(ValueError):
    pass


def validate_loopback_url(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path.startswith("/")):
        raise UnsafeTargetError("prototype accepts only explicit http://127.0.0.1 or http://[::1] MCP endpoints")
    try:
        if parsed.port is None:
            raise ValueError("port required")
    except ValueError as exc:
        raise UnsafeTargetError("explicit valid port required") from exc
    return url


def _client_factory(headers=None, timeout=None, auth=None):
    # SDK default follows redirects, which could escape the loopback restriction.
    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                             follow_redirects=False, trust_env=False)


async def _catalog(session: ClientSession) -> tuple[list[dict[str, Any]], list[types.Tool]]:
    cursor: str | None = None
    all_tools: list[types.Tool] = []
    for _ in range(10):
        page = await session.list_tools(cursor=cursor)
        all_tools.extend(page.tools)
        if len(all_tools) > 200:
            raise UnsafeTargetError("tool catalog exceeds 200 entries")
        cursor = page.nextCursor
        if not cursor:
            break
    else:
        raise UnsafeTargetError("tool catalog pagination limit reached")
    snapshot = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in all_tools]
    if len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) > 1_000_000:
        raise UnsafeTargetError("tool catalog exceeds 1 MB")
    return snapshot, all_tools


async def probe_loopback(url: str) -> list[dict[str, Any]]:
    validate_loopback_url(url)

    async def run():
        async with streamablehttp_client(url, timeout=5, sse_read_timeout=5,
                                         httpx_client_factory=_client_factory) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                snapshot, _ = await _catalog(session)
                return snapshot

    return await asyncio.wait_for(run(), timeout=20)


async def list_observed(url: str, baseline: dict[str, Any] | None = None
                        ) -> tuple[list[types.Tool], list[str]]:
    validate_loopback_url(url)
    snapshot = await probe_loopback(url)
    changes = compare_baseline(assess(url, snapshot), baseline) if baseline is not None else []
    signals = ["catalog_changed"] if changes else []
    return [types.Tool.model_validate(tool) for tool in snapshot], signals


async def call_observed(url: str, tool_name: str, arguments: dict[str, Any],
                        baseline: dict[str, Any] | None = None
                        ) -> tuple[types.CallToolResult, list[str]]:
    """Forward an explicitly requested call and report observed risk signals.

    No security finding blocks or changes the call/result. This is a scanner,
    not a protective gateway; tool execution may have real side effects.
    """
    validate_loopback_url(url)
    signals: set[str] = set()
    if inspect_runtime_payload(tool_name, arguments):
        signals.add("sensitive_argument")

    async def run():
        async with streamablehttp_client(url, timeout=5, sse_read_timeout=5,
                                         httpx_client_factory=_client_factory) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                snapshot, _ = await _catalog(session)
                if baseline is not None and compare_baseline(assess(url, snapshot), baseline):
                    signals.add("catalog_changed")
                result = await session.call_tool(tool_name, arguments)
                if any(not isinstance(block, types.TextContent) for block in result.content):
                    signals.add("uninspectable_result")
                text = "\n".join(block.text for block in result.content if isinstance(block, types.TextContent))
                if result.structuredContent is not None:
                    text += "\n" + json.dumps(result.structuredContent, ensure_ascii=False)
                if len(text.encode("utf-8")) > 64_000:
                    signals.add("uninspectable_result")
                if inspect_runtime_payload(tool_name, response_text=text[:64_000]):
                    signals.add("poisoned_result")
                return result

    result = await asyncio.wait_for(run(), timeout=30)
    return result, sorted(signals)
