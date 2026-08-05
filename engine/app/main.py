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

# 이 엔진은 인증이 없다 — 127.0.0.1 에 떠 있으니 "내 기계 안" 이면 다 우리 편이라는
# 전제였다. 브라우저에서는 그 전제가 성립하지 않는다: 사용자가 열어둔 아무 웹페이지나
# 그 페이지의 JS 로 http://127.0.0.1:48200 에 요청을 보낼 수 있고, allow_origins=["*"]
# 는 **그 응답을 읽는 것까지** 허락한다. 포트 후보도 10개뿐이라 찾는 데 시간도 안 걸린다.
#
# 파일을 읽어주는 /mcp 는 MCP SDK 자체의 DNS rebinding 보호가 Origin 을 걸러주지만
# (실측: "Invalid Origin header"), 여기 REST 라우터는 아무도 안 막고 있었다. 그래서
# 임의의 사이트가 할 수 있던 것:
#   - Campfire 설치 여부와 /health 의 엔진 상태·포트·모델 준비 상황을 그대로 읽기
#   - /jobs, /jobs/prompt 로 로컬 ML 파이프라인을 마음대로 돌리기. 인젝션이 잡히면
#     2단계 위치 특정이 Upstage API 를 호출하므로 **사용자 API 키로 과금**까지 된다.
#   - /models/fetch 로 수백 MB 다운로드를 유발
# 사용자 파일을 읽어가는 수준은 아니지만, 아무 사이트나 부를 수 있어야 할 이유도 없다.
#
# 그래서 응답을 읽을 수 있는 출처를 확장(chrome-extension://<id>)으로 한정한다. 웹페이지도
# 단순 요청(multipart POST 등)을 "보내는" 것 자체는 여전히 막지 못하지만(CORS 의 원래
# 한계다), 응답을 못 읽으므로 결과를 가져가지는 못한다.
#
# 앱/데스크탑·MCP 클라이언트는 브라우저가 아니라 Origin 을 아예 안 붙이므로 영향 없다.
EXTENSION_ORIGIN_PATTERN = r"^(chrome|moz|safari-web)-extension://[A-Za-z0-9._-]+$"

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=EXTENSION_ORIGIN_PATTERN,
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
