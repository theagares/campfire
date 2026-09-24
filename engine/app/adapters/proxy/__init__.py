"""
app/adapters/proxy
로컬 TLS 프록시 어댑터. http_api·mcp 옆에 서는 세 번째 입구다.

엔진과 **같은 프로세스·같은 이벤트 루프**에서 돈다. 이게 이 방식이 싼 이유다 —
애드온이 붙든 요청과 사람이 누르는 판단이 같은 메모리 안의 Future 하나로 이어져서
IPC 도 폴링도 없다(decision.py 참고).

켜고 끄는 길이 둘이다:
  - 기동 시 자동: SECUREDOC_PROXY_ENABLED=1 (lifespan 이 start() 를 부른다)
  - 실행 중 토글: POST /proxy/start · /proxy/stop (start(force=True))
시스템 프록시(브라우저가 여기로 오게 하는 설정)는 엔진이 아니라 데스크탑 앱이 만진다.
엔진은 강제 종료(taskkill /F)로 끝나서 스스로 설정을 되돌릴 기회가 없기 때문이다.

mitmproxy 는 선택 의존성이다(`pip install -e ".[proxy]"`). 설치돼 있지 않으면
프록시만 조용히 꺼지고 나머지 엔진은 그대로 뜬다 — 기존 확장 경로를 쓰는 설치가
프록시 때문에 기동에 실패하면 안 된다.
"""

from __future__ import annotations

import asyncio
import logging

from app import config

logger = logging.getLogger("securedoc.proxy")

_task: asyncio.Task | None = None
_master = None

# 바인딩을 기다리는 상한. 넘으면 실패로 본다.
_BIND_TIMEOUT_S = 5.0


def _listen_addrs() -> list:
    """mitmproxy 가 실제로 붙잡고 있는 주소. 비어 있으면 듣고 있지 않다."""
    if _master is None:
        return []
    ps = _master.addons.get("proxyserver")
    if ps is None:
        return []
    addrs = ps.listen_addrs
    return list(addrs() if callable(addrs) else addrs)


def is_running() -> bool:
    """지금 요청을 받을 수 있는가.

    태스크가 살아 있는 것만으로는 부족하다 — mitmproxy 는 포트 바인딩에 실패해도
    로그만 남기고 계속 돈다. 그 상태를 "켜짐" 으로 보고 시스템 프록시를 여기로
    돌리면 브라우저가 죽은 포트를 향한다. 실제로 붙잡은 주소가 있을 때만 True.
    """
    return _task is not None and not _task.done() and bool(_listen_addrs())


def ca_cert_path() -> str:
    """mitmproxy 가 만든 CA 인증서 경로. 처음 한 번 기동해야 파일이 생긴다."""
    from pathlib import Path

    return str(Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.cer")


def ca_trusted() -> bool | None:
    """우리 CA 가 Windows 신뢰 저장소에 있는가. 판단할 수 없으면 None.

    시스템 프록시를 켜기 전 마지막 관문이다. CA 가 없는데 브라우저를 여기로 돌리면
    AI 사이트가 전부 인증서 오류로 열리지 않는다 — 켜기 전에 막아야 한다.

    ssl.enum_certificates 는 CurrentUser 저장소까지 본다(사용자가 관리자 권한 없이
    certutil -user 로 넣은 것도 잡힌다 — 실측).
    """
    import ssl
    from pathlib import Path

    if not hasattr(ssl, "enum_certificates"):
        return None  # Windows 가 아니다
    p = Path(ca_cert_path())
    if not p.exists():
        return False
    raw = p.read_bytes()
    try:
        der = ssl.PEM_cert_to_DER_cert(raw.decode("ascii")) if raw.startswith(b"-----BEGIN") else raw
    except Exception:
        return False
    return any(cert == der for cert, _enc, _trust in ssl.enum_certificates("ROOT"))


def status() -> dict:
    """앱이 토글을 그릴 때 보는 값. health 에도 같은 것이 실린다."""
    import os
    import sys

    try:
        from app.adapters.proxy.addon import intercept_hosts

        exact, suffixes = intercept_hosts()
    except ImportError:  # mitmproxy 미설치
        exact, suffixes = [], []
    return {
        "supported": sys.platform == "win32",
        # 앱이 PAC 를 만들 때 쓴다 — 이 호스트만 프록시로, 나머지는 직접.
        "hosts": {"exact": exact, "suffixes": suffixes},
        "running": is_running(),
        "port": config.PROXY_PORT,
        "caPath": ca_cert_path(),
        "caExists": os.path.exists(ca_cert_path()),
        "caTrusted": ca_trusted(),
    }


async def start(*, force: bool = False) -> bool:
    """프록시를 띄운다. 실제로 포트를 붙잡았으면 True.

    force=False 는 기동 시 자동 시작 경로다(PROXY_ENABLED 를 따른다).
    force=True 는 토글 경로다(사용자가 켜라고 했으니 설정값과 무관하게 띄운다).

    실패(미설치·포트 충돌)는 예외로 올리지 않는다. 프록시는 부가 기능이고
    엔진을 멈출 이유가 없다.
    """
    global _task, _master
    if not force and not config.PROXY_ENABLED:
        return False
    if is_running():
        return True
    if _task is not None:
        # 태스크는 남았는데 듣고 있지 않다(바인딩 실패 후 좀비). 치우고 다시 띄운다.
        await stop()
    try:
        from mitmproxy.addons import default_addons
        from mitmproxy.master import Master
        from mitmproxy.options import Options

        from app.adapters.proxy.addon import CampfireAddon, intercept_host_patterns
    except ImportError:
        logger.info('[proxy] mitmproxy 미설치 — 프록시 비활성. pip install -e ".[proxy]"')
        return False

    try:
        opts = Options(listen_host="127.0.0.1", listen_port=config.PROXY_PORT)
        _master = Master(opts, event_loop=asyncio.get_running_loop())
        _master.addons.add(*default_addons())
        # 아래 두 옵션은 기본 애드온이 등록하는 것이라 Options() 생성 시점엔 아직 없다
        # (mitmproxy 11 에서 KeyError). 애드온을 올린 뒤에 설정한다.
        opts.update(
            # 스트리밍 안 함. 본문을 통째로 받아야 검사할 수 있다 — 켜지면 request 훅이
            # 본문 도착 전에 불려 빈 바이트를 검사한다. 기본값도 None 이지만 못 박아 둔다.
            stream_large_bodies=None,
            # AI 사이트만 복호화하고 나머지는 터널로 그냥 넘긴다(intercept_host_patterns).
            allow_hosts=intercept_host_patterns(),
        )
        _master.addons.add(CampfireAddon())
        _task = asyncio.create_task(_master.run())
    except Exception:
        logger.exception("[proxy] 기동 실패 — 프록시 없이 계속한다")
        await stop()
        return False

    # 바인딩은 run() 안에서 비동기로 일어난다. 붙잡을 때까지 기다린다 — 여기서 바로
    # True 를 돌려주면, 포트를 못 잡았는데도 호출자가 시스템 프록시를 여기로 돌린다.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _BIND_TIMEOUT_S
    while loop.time() < deadline:
        if _task.done():
            break
        if _listen_addrs():
            logger.info("[proxy] 기동 — 127.0.0.1:%s", config.PROXY_PORT)
            return True
        await asyncio.sleep(0.05)

    logger.error("[proxy] 포트 %s 를 붙잡지 못했다 — 기동 취소", config.PROXY_PORT)
    await stop()
    return False


async def stop() -> None:
    """프록시를 내린다. 붙들고 있던 판단은 전부 차단으로 끝낸다."""
    global _task, _master
    from app.adapters.proxy.decision import broker

    # 먼저 판단을 끝낸다. 루프가 내려간 뒤에는 브라우저 요청이 타임아웃까지 매달린다.
    n = broker.cancel_all()
    if n:
        logger.info("[proxy] 중지 — 대기 중이던 %d건을 차단으로 끝냈다", n)

    if _master is not None:
        # 리스너를 **먼저** 내린다. mitmproxy 11 은 shutdown() 만으로는 포트를 놓지
        # 않는다 — run() 태스크가 끝나도 listen_addrs 가 그대로 남고, 같은 포트로
        # 다시 켜면 바인딩에 실패한다(실측). 그러면 토글을 한 번 끄면 다시 못 켠다.
        # server=False 가 Proxyserver 의 configure 를 거쳐 리스너를 닫는다.
        try:
            _master.options.update(server=False)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _BIND_TIMEOUT_S
            while _listen_addrs() and loop.time() < deadline:
                await asyncio.sleep(0.05)
            if _listen_addrs():
                logger.error("[proxy] 리스너가 내려가지 않았다: %s", _listen_addrs())
        except Exception:
            logger.exception("[proxy] 리스너 중지 실패")
        try:
            _master.shutdown()
        except Exception:
            logger.exception("[proxy] shutdown 실패")
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            _task.cancel()
    _task = None
    _master = None
