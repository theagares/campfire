"""
app/adapters/mcp/stdio_shim.py
stdio → HTTP 중계 shim (PLAN §4).

stdio 만 지원하는 MCP 클라이언트(예: Claude Desktop)를 위해, 클라이언트와는 stdio 로
말하고 실제 요청은 이미 떠 있는 엔진의 Streamable HTTP `/mcp` 엔드포인트로 그대로
중계하는 얇은 런처. 자체 엔진을 새로 띄우지 않고 "엔진 프로세스 하나를 공유"한다는
원칙(PLAN §4)을 지킨다 — 이 스크립트는 트랜스포트 브리지일 뿐 도구를 직접 실행하지 않는다.

동작: stdin/stdout(JSON-RPC) ↔ HTTP(/mcp) 사이에서 SessionMessage 를 양방향으로 펌프.

사용법 (Claude Desktop claude_desktop_config.json 예):
    {
      "mcpServers": {
        "securedoc-gateway": {
          "command": "D:\\...\\engine\\.venv\\Scripts\\python.exe",
          "args": ["-m", "app.adapters.mcp.stdio_shim"],
          "cwd": "D:\\...\\engine",
          "env": {"SECUREDOC_MCP_URL": "http://127.0.0.1:48200/mcp"}
        }
      }
    }

환경변수:
    SECUREDOC_MCP_URL  중계 대상 엔진 MCP URL (기본: http://127.0.0.1:48200/mcp)
                       엔진이 다른 포트(48200~48209)에 떴다면 이 값으로 지정.
"""

from __future__ import annotations

import os
import sys

import anyio
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

DEFAULT_URL = "http://127.0.0.1:48200/mcp"


async def _pump(src, dst, label: str) -> None:
    """src(receive stream)에서 SessionMessage 를 받아 dst(send stream)로 전달."""
    async for item in src:
        if isinstance(item, Exception):
            # 트랜스포트 레벨 오류는 stderr 로만 남기고 계속(브리지는 투명해야 함)
            print(f"[stdio_shim] {label} 오류: {item!r}", file=sys.stderr)
            continue
        if isinstance(item, SessionMessage):
            await dst.send(item)


async def _run(url: str) -> None:
    async with streamablehttp_client(url) as (http_read, http_write, _get_sid):
        async with stdio_server() as (stdio_read, stdio_write):
            async with anyio.create_task_group() as tg:
                # 클라이언트(stdio) → 엔진(HTTP)
                tg.start_soon(_pump, stdio_read, http_write, "stdio→http")
                # 엔진(HTTP) → 클라이언트(stdio)
                tg.start_soon(_pump, http_read, stdio_write, "http→stdio")


def main() -> None:
    url = os.environ.get("SECUREDOC_MCP_URL", DEFAULT_URL)
    print(f"[stdio_shim] 중계 대상: {url}", file=sys.stderr)
    try:
        anyio.run(_run, url)
    except (KeyboardInterrupt, EOFError):
        pass


if __name__ == "__main__":
    main()
