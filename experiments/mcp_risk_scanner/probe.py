"""Opt-in loopback MCP metadata probe and observation-only forwarding.

Only numeric loopback addresses are supported in this prototype. Arbitrary remote
URLs would require DNS rebinding/redirect-resistant egress controls.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from .core import (
    MAX_RUNTIME_INSPECTION_BYTES,
    _utf8_prefix,
    assess,
    compare_baseline,
    inspect_runtime_payload,
)


class UnsafeTargetError(ValueError):
    pass


def _leaf_error(exc: BaseException) -> BaseException:
    """Extract a concise cause from AnyIO task-group exception wrappers."""
    current = exc
    while True:
        nested = getattr(current, "exceptions", None)
        if not isinstance(nested, tuple) or not nested:
            return current
        current = nested[0]


def _error_detail(exc: BaseException) -> str:
    raw = str(exc)[:300]
    return "".join(char if char.isprintable() and char not in {"\u202d", "\u202e"}
                   else f"\\u{ord(char):04x}" for char in raw)


async def _run_target(awaitable, *, timeout: float, operation: str):
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.CancelledError:
        raise
    except UnsafeTargetError:
        raise
    except Exception as exc:
        cause = _leaf_error(exc)
        detail = _error_detail(cause)
        raise UnsafeTargetError(f"{operation} failed: {detail or type(cause).__name__}") from exc


def validate_loopback_url(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path.startswith("/")):
        raise UnsafeTargetError("prototype accepts only explicit http://127.0.0.1 or http://[::1] MCP endpoints")
    try:
        if not parsed.port:
            raise ValueError("port required")
    except ValueError as exc:
        raise UnsafeTargetError("explicit valid port required") from exc
    return url


def _http_client() -> httpx.AsyncClient:
    # Redirects and ambient proxies could escape the loopback restriction.
    return httpx.AsyncClient(timeout=httpx.Timeout(5), follow_redirects=False,
                             trust_env=False)


@asynccontextmanager
async def loopback_session(url: str):
    """Keep one initialized upstream session for a probe or proxy lifetime."""
    validate_loopback_url(url)
    try:
        async with _http_client() as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                async with read, write:
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
    except asyncio.CancelledError:
        raise
    except UnsafeTargetError:
        raise
    except Exception as exc:
        cause = _leaf_error(exc)
        detail = _error_detail(cause)
        raise UnsafeTargetError(f"loopback MCP session failed: {detail or type(cause).__name__}") from exc


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
        async with loopback_session(url) as session:
            snapshot, _ = await _catalog(session)
            return snapshot

    return await _run_target(run(), timeout=20, operation="loopback MCP probe")


async def list_observed(url: str, baseline: dict[str, Any] | None = None,
                        baseline_key: bytes | None = None,
                        session: ClientSession | None = None,
                        ) -> tuple[list[types.Tool], list[str]]:
    validate_loopback_url(url)
    if session is None:
        snapshot = await probe_loopback(url)
    else:
        snapshot, _ = await _run_target(_catalog(session), timeout=20,
                                        operation="loopback MCP catalog read")
    changes = (compare_baseline(assess(url, snapshot), baseline,
                                integrity_key=baseline_key,
                                require_integrity=baseline_key is not None)
               if baseline is not None else [])
    signals = ["catalog_changed"] if changes else []
    return [types.Tool.model_validate(tool) for tool in snapshot], signals


async def call_observed(url: str, tool_name: str, arguments: dict[str, Any],
                        baseline: dict[str, Any] | None = None,
                        baseline_key: bytes | None = None,
                        session: ClientSession | None = None,
                        ) -> tuple[types.CallToolResult, list[str]]:
    """Forward an explicitly requested call and report observed risk signals.

    No security finding blocks or changes the call/result. This is a scanner,
    not a protective gateway; tool execution may have real side effects.
    """
    validate_loopback_url(url)
    signals: set[str] = set()
    argument_findings = inspect_runtime_payload(tool_name, arguments)
    if any(finding.code != "uninspectable_argument" for finding in argument_findings):
        signals.add("sensitive_argument")
    if any(finding.code == "uninspectable_argument" for finding in argument_findings):
        signals.add("uninspectable_argument")

    def inspected_result_text(result: types.CallToolResult) -> tuple[str, bool]:
        def text_chunks():
            first = True
            for block in result.content:
                if not isinstance(block, types.TextContent):
                    continue
                if not first:
                    yield "\n"
                yield block.text
                first = False

        chunks = text_chunks()
        parts: list[str] = []
        remaining = MAX_RUNTIME_INSPECTION_BYTES
        truncated = any(not isinstance(block, types.TextContent) for block in result.content)

        def append_chunks(values) -> bool:
            nonlocal remaining, truncated
            iterator = iter(values)
            for value in iterator:
                prefix, used, cut = _utf8_prefix(value, remaining)
                parts.append(prefix)
                remaining -= used
                if cut:
                    truncated = True
                    return False
                if remaining == 0:
                    try:
                        next(iterator)
                    except StopIteration:
                        return True
                    truncated = True
                    return False
            return True

        if append_chunks(chunks) and result.structuredContent is not None:
            append_chunks(iter(("\n",)))
            if remaining:
                encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
                append_chunks(encoder.iterencode(result.structuredContent))
        return "".join(parts), truncated

    async def perform(active_session: ClientSession):
        snapshot, _ = await _catalog(active_session)
        if (baseline is not None
                and compare_baseline(assess(url, snapshot), baseline,
                                     integrity_key=baseline_key,
                                     require_integrity=baseline_key is not None)):
            signals.add("catalog_changed")
        result = await active_session.call_tool(tool_name, arguments)
        try:
            text, truncated = inspected_result_text(result)
            result_findings = inspect_runtime_payload(tool_name, response_text=text)
        except (TypeError, ValueError, RecursionError, UnicodeError):
            text, truncated, result_findings = "", True, []
        if truncated:
            signals.add("uninspectable_result")
        if any(finding.code != "uninspectable_result" for finding in result_findings):
            signals.add("poisoned_result")
        return result

    if session is None:
        async def run():
            async with loopback_session(url) as active_session:
                return await perform(active_session)

        result = await _run_target(run(), timeout=30, operation="loopback MCP call")
    else:
        result = await _run_target(perform(session), timeout=30, operation="loopback MCP call")
    return result, sorted(signals)
