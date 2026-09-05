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
# 확장 프로그램 오리진만 허용한다 (chrome-extension://<32자 ID>).
#
# 예전 값은 allow_origins=["*"] 였다. 이 API 는 인증이 없고 GET 두 번이면 최근 job 의
# 결과(originalText 포함)까지 닿을 수 있는데, "*" 는 **아무 웹사이트나** 그 응답을 읽어도
# 좋다고 브라우저에 명시적으로 허락하는 값이다. 로컬 서비스에서 이건 공짜로 주는 권한이다.
#
# 정상 호출자는 "*" 가 필요 없다는 것을 확인했다:
#   - 확장: engine 을 부르는 곳은 background/service-worker.js 하나뿐이고, 사이드패널·
#     팝업·content 는 chrome.runtime 메시징만 쓴다.
#   - 데스크탑: main 프로세스(Node http/fetch)만 부른다 — 브라우저 컨텍스트가 아니라
#     CORS 대상이 아니고, Origin 헤더도 안 보내므로 이 미들웨어가 개입하지 않는다.
#   - MCP 클라이언트: 브라우저가 아니다.
#
# 정규식을 쓰는 이유: manifest.json 에 key 가 없어 확장 ID 가 설치 방식(스토어/언팩)에
# 따라 달라진다. 고정 ID 를 박으면 개발 설치에서 조용히 깨진다. Chrome 확장 ID 는 a-p
# 32자라 그 형태만 허용한다 — http(s) 웹사이트는 어떤 것도 통과하지 못한다.
#
# 이건 브라우저 벡터만 닫는다. 같은 PC 의 다른 프로세스는 CORS 와 무관하게 그대로
# 접근할 수 있다 — 그쪽은 토큰 인증이 필요하고 별도 작업이다.
EXTENSION_ORIGIN_REGEX = r"^chrome-extension://[a-p]{32}$"

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=EXTENSION_ORIGIN_REGEX,
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
