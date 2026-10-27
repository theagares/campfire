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

# 앱·확장이 몇 초마다 부르는 상태 확인 요청. 성공 응답까지 접근 로그에 남기면 로그가 분당
# 수백 줄씩 자란다(앱의 판단 폴링 0.5초, 연결 화면 1.5초, 확장 /health·경로 보고). 실패
# 응답은 원인 추적에 필요하니 남긴다.
_QUIET_POLL_PATHS = frozenset({
    "/health", "/decisions", "/models/status", "/proxy/status", "/proxy/extension-route",
})


class _QuietPollingFilter(logging.Filter):
    """uvicorn 접근 로그에서 상태 확인 폴링의 성공 응답을 뺀다."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access: (client_addr, method, path_with_query, http_version, status_code)
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path = str(args[2]).split("?", 1)[0]
        status = args[4]
        # 판단 창이 떠 있는 동안 앱이 1초마다 그 건이 남아 있는지 본다(GET /decisions/<id>) —
        # 한 건이 만료까지 가면 약 120줄이 쌓였다(맥 실측). 결정(POST)과 실패 응답은 남긴다.
        polled = path in _QUIET_POLL_PATHS or (args[1] == "GET" and path.startswith("/decisions/"))
        return not (polled and isinstance(status, int) and status < 400)


def _uvicorn_log_config() -> dict:
    import copy

    from uvicorn.config import LOGGING_CONFIG

    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config.setdefault("filters", {})["quiet_polling"] = {"()": _QuietPollingFilter}
    log_config["loggers"]["uvicorn.access"]["filters"] = ["quiet_polling"]
    return log_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.adapters.mcp import mcp_session_context
    from app.adapters.mcp_risk_scanner import risk_scanner_context
    from app.core.detectors import registry
    from app.models_sync import sync_bundled_model_files
    from app.store import db

    # detector 를 로드하기 전에 해야 한다 — 런타임 스크립트가 보관 위치에 있어야
    # 서브프로세스를 띄울 수 있다(models_sync 모듈 docstring 참고).
    sync_bundled_model_files()
    registry.load_detectors()
    db.init_db()
    logger.info(
        "[engine] 기동 완료 — detectors=%s, policy=%s, port=%s, edition=%s",
        registry.active_detectors(),
        config.INJECTION_POLICY,
        config.BOUND_PORT,
        "lite" if config.LITE else "full",
    )
    # 프록시 게이트웨이(adapters/proxy)는 같은 루프에 얹는다 — 애드온이 붙든 요청과
    # 사람의 판단이 메모리 안의 Future 하나로 이어져야 IPC 없이 HITL 이 된다.
    # 꺼져 있거나 mitmproxy 가 없으면 조용히 건너뛴다.
    from app.adapters import proxy

    await proxy.start()
    try:
        # 위험 검사기는 별도 stdio MCP 프로세스다. 기본 비활성이고, 기동 실패도 기존
        # 엔진과 대상 MCP 사용을 막지 않는다. 앱에는 이 좁은 어댑터 외의 구현 의존성이 없다.
        async with risk_scanner_context() as risk_scanner:
            app.state.mcp_risk_scanner = risk_scanner
            if config.LITE:  # 라이트 판은 MCP 서버를 열지 않는다(아래 mount_mcp 도 건너뛴다)
                yield
            else:
                # MCP session manager 를 lifespan 동안 기동 (PLAN §4, /mcp Streamable HTTP)
                async with mcp_session_context():
                    yield
    finally:
        try:
            await proxy.stop()
        finally:
            try:
                # 파서 워커는 지연 생성되지만 한 번 뜨면 재사용된다. lifespan 종료 때
                # 명시적으로 내려 개발 재시작과 앱 종료 뒤 자식 프로세스가 남지 않게 한다.
                from app.core.parser.process_pool import shutdown_default_parse_pool

                shutdown_default_parse_pool()
            finally:
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

if not config.LITE:  # 라이트 판(config.LITE)은 MCP 를 쓰지 않는다
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


def exit_with_parent() -> None:
    """데스크톱 앱이 사라지면 엔진도 끈다 — 앱이 stdin 파이프를 줄 때만(config.EXIT_WITH_PARENT).

    앱이 강제 종료·비정상 종료되면 종료 정리(engine-manager stop)가 돌지 않아 엔진이 부모 없이 남았다.
    남은 엔진은 예전 강제 마스킹 단어(또는 빈 목록)를 든 채 가장 낮은 포트를 쥐고 있어 확장이 그리로
    검사를 보냈고, 새 앱의 엔진은 다음 포트로 밀렸다 — 등록 단어가 하나도 안 가려지고 앱 처리현황도
    안 움직였다(맥 실측: PPID 1 엔진이 48200 에 단어 0개, 앱 엔진은 48201 에 11개).
    앱은 이 파이프에 아무것도 쓰지 않는다. 앱 프로세스가 끝나면 운영체제가 파이프를 닫아 EOF 가 온다.
    """
    import os
    import signal
    import sys
    import threading
    import time

    def watch() -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError, AttributeError):
            return  # stdin 을 못 읽으면 감시만 포기한다 — 엔진을 끄지는 않는다
        logger.warning("[engine] 데스크톱 앱이 사라져 엔진을 끕니다")
        signal.raise_signal(signal.SIGTERM)  # uvicorn 정상 종료 — 프록시·검출기 자식 정리
        time.sleep(10)
        os._exit(0)  # 정상 종료가 멈춰도 포트는 놓는다

    threading.Thread(target=watch, name="exit-with-parent", daemon=True).start()


def main() -> None:
    import uvicorn

    if config.EXIT_WITH_PARENT:
        exit_with_parent()
    port = find_available_port()
    config.BOUND_PORT = port
    logger.info("[engine] %s:%d 에서 시작합니다 (service=%s)", config.HOST, port, config.SERVICE_NAME)
    if config.DEMO_TRACE_ENABLED:
        logger.warning("[engine] 시연 추적 켜짐 — 대시보드 http://%s:%d/demo/dashboard (가린 문장을 메모리에 둠)",
                       config.HOST, port)
    uvicorn.run(app, host=config.HOST, port=port, log_level="info", log_config=_uvicorn_log_config())


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
