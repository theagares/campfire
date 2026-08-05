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


async def _send_plain(send: Send, status: int, body: str) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
    await send({"type": "http.response.body", "body": body.encode()})


def _has_origin_header(scope: Scope) -> bool:
    """요청에 Origin 헤더가 있는가 = 브라우저가 보낸 요청인가.

    Origin 은 브라우저가 스스로 붙이는 헤더이고 페이지 스크립트가 지울 수 없다. 반대로
    MCP 클라이언트(Claude Desktop, stdio_shim 등)는 브라우저가 아니라 붙이지 않는다.
    그래서 "Origin 이 있다" = "브라우저 탭에서 온 요청" 으로 봐도 된다.
    """
    return any(name.lower() == b"origin" for name, _ in scope.get("headers", []))


async def _mcp_asgi(scope: Scope, receive: Receive, send: Send) -> None:
    """`/mcp` 로 온 요청을 현재 활성 session manager 로 위임한다.

    session manager 가 아직 기동 전이면 503 을 돌려준다(정상 부팅 후엔 항상 존재).

    그 전에 브라우저에서 온 요청을 먼저 끊는다. 여기 붙은 도구들은 로컬 파일을 읽고
    (secure_read_file/scan_file) 디렉터리를 나열한다(secure_list_files) — 엔진에 인증이
    없으므로 이 경로가 브라우저에 열리면 곧바로 파일 접근이 열린다. CORS 를 조여도
    (main.py) 응답을 못 읽게 될 뿐 호출 자체는 막히지 않으니, 여기서 요청 단계에 거절한다.

    현재 설치본에서는 MCP SDK 의 DNS rebinding 보호가 이미 같은 일을 하고 있다(실측:
    Origin 을 붙이면 SDK 가 "Invalid Origin header" 로 403). 그래도 우리 쪽에 두는 이유는
    그게 **우리 코드가 아니라 SDK 기본값**이기 때문이다 — pyproject 의 허용 범위가
    `mcp>=1.2,<2.0` 로 넓어서 어떤 1.x 가 설치되느냐에 따라 그 미들웨어의 유무·기본값이
    달라질 수 있다(SDK 안에서도 미들웨어를 인자 없이 만들면 보호가 꺼진 채로 시작한다).
    파일 접근이 걸린 경로의 안전이 의존성 해석 결과에 좌우되게 두지 않는다.
    """
    if _has_origin_header(scope) and scope["type"] == "http":
        await _send_plain(
            send, 403,
            "MCP 엔드포인트는 브라우저에서 호출할 수 없습니다 "
            "(로컬 파일 접근 도구가 붙어 있어 웹페이지에 노출하지 않습니다).",
        )
        return

    manager = mcp._session_manager  # noqa: SLF001 - 어댑터에서만 접근하는 내부 핸들
    if manager is None:
        if scope["type"] == "http":
            await _send_plain(send, 503, "MCP 세션 매니저 미기동")
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
