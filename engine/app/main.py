"""
app/main.py
FastAPI 엔트리 + 포트 자동 스캔(48200~48209, PLAN §11) + lifespan.

실행:
  python -m app.main
  (또는)  uvicorn app.main:app --host 127.0.0.1 --port 48200
"""

from __future__ import annotations

import contextlib
import logging
import socket
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("securedoc.engine")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.adapters.mcp import mcp_session_context
    from app.core.detectors import registry
    from app.models_sync import sync_bundled_model_files
    from app.store import db

    # detector 를 로드하기 전에 해야 한다 — 런타임 스크립트가 보관 위치에 있어야
    # 서브프로세스를 띄울 수 있다(models_sync 모듈 docstring 참고).
    sync_bundled_model_files()
    registry.load_detectors()
    db.init_db()
    logger.info(
        "[engine] 기동 완료 — detectors=%s, policy=%s, port=%s",
        registry.active_detectors(),
        config.INJECTION_POLICY,
        config.BOUND_PORT,
    )
    # MCP session manager 를 lifespan 동안 기동 (PLAN §4, /mcp Streamable HTTP)
    async with mcp_session_context():
        yield
    db.close_db()


app = FastAPI(title="Campfire engine", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

from app.adapters.http_api import router  # noqa: E402

app.include_router(router)

# MCP Streamable HTTP 엔드포인트를 /mcp 에 마운트 (PLAN §4·§9) — REST 와 프로세스 공유
from app.adapters.mcp import mount_mcp  # noqa: E402

mount_mcp(app)


# ── 포트 자동 스캔 (PLAN §11) ─────────────────────────────────────────────────
def find_available_port() -> int:
    """BASE_PORT 부터 EADDRINUSE 시 +1 씩 최대 PORT_SCAN_COUNT 개까지 시도."""
    last_port = config.BASE_PORT + config.PORT_SCAN_COUNT - 1
    for port in range(config.BASE_PORT, last_port + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((config.HOST, port))
            except OSError:
                logger.info("[engine] 포트 %d 사용 중 — 다음 포트 시도", port)
                continue
            return port
    raise RuntimeError(
        f"포트 {config.BASE_PORT}~{last_port} 가 모두 사용 중입니다. "
        "이전에 남은 좀비 프로세스가 있는지 확인 후 재시도하세요."
    )


def main() -> None:
    import uvicorn

    port = find_available_port()
    config.BOUND_PORT = port
    logger.info("[engine] %s:%d 에서 시작합니다 (service=%s)", config.HOST, port, config.SERVICE_NAME)
    uvicorn.run(app, host=config.HOST, port=port, log_level="info")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
