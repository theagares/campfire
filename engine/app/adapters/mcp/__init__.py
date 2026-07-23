"""
app/adapters/mcp/
MCP 어댑터 (PLAN §2·§4·§9) — Streamable HTTP MCP 엔드포인트를 기존 FastAPI 앱에
`/mcp` 로 마운트한다. 엔진 프로세스 하나를 REST 와 MCP 가 공유한다(별도 프로세스 없음).

transport 선택: MCP Python SDK(FastMCP)의 Streamable HTTP transport.
  - stateless_http=True 로 세션 상태를 요청 간 유지하지 않아 FastAPI sub-app 마운트가 단순·견고.
  - stdio 전용 클라이언트(Claude Desktop 등)는 stdio_shim.py 가 이 HTTP 엔드포인트로 중계.

마운트 방법:
  1) main.py 가 `mount_mcp(app)` 로 `/mcp` 에 얇은 위임(delegating) ASGI 앱을 마운트.
     이 위임 앱은 매 요청마다 "현재 살아있는" session manager 로 넘긴다.
  2) FastMCP 의 session manager 는 lifespan 안에서만 살아있어야 하므로,
     main.py 의 lifespan 이 `mcp_session_context()` 를 async with 로 감싼다.
     session manager 는 1회용(run() 재호출 불가)이라, lifespan 진입마다 새로 만들어
     프로세스 내 재기동(테스트에서 TestClient→uvicorn 순차 부팅 등)에도 안전하게 한다.
"""

from __future__ import annotations

import contextlib
from typing import AsyncIterator

from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from .tools import mcp

# FastMCP streamable_http_path 기본이 "/mcp" 라 "/" 로 바꿔 라우팅을 위임 앱에 맡긴다.
mcp.settings.streamable_http_path = "/"

MCP_PATH = "/mcp"


async def _mcp_asgi(scope: Scope, receive: Receive, send: Send) -> None:
    """`/mcp` 로 온 요청을 현재 활성 session manager 로 위임한다.

    session manager 가 아직 기동 전이면 503 을 돌려준다(정상 부팅 후엔 항상 존재).
    """
    manager = mcp._session_manager  # noqa: SLF001 - 어댑터에서만 접근하는 내부 핸들
    if manager is None:
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body", "body": "MCP 세션 매니저 미기동".encode()})
        return
    await manager.handle_request(scope, receive, send)


def mount_mcp(app: FastAPI) -> None:
    """FastAPI 앱의 `/mcp` 에 MCP Streamable HTTP 위임 앱을 마운트."""
    app.mount(MCP_PATH, _mcp_asgi)


@contextlib.asynccontextmanager
async def mcp_session_context() -> AsyncIterator[None]:
    """MCP session manager 를 (재)생성·기동/종료한다. main.py lifespan 에서 감싼다.

    StreamableHTTPSessionManager.run() 은 인스턴스당 1회만 호출 가능하므로, 매 진입마다
    기존 매니저를 버리고 streamable_http_app() 으로 새로 만들어 재기동 가능하게 한다.
    """
    mcp._session_manager = None  # noqa: SLF001 - 재기동을 위해 1회용 매니저 재생성
    mcp.streamable_http_app()  # 세션 매니저 lazy 생성
    async with mcp.session_manager.run():
        yield


__all__ = ["mcp", "mount_mcp", "mcp_session_context", "MCP_PATH"]
