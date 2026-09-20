"""
app/adapters/proxy
로컬 TLS 프록시 어댑터. http_api·mcp 옆에 서는 세 번째 입구다.

엔진과 **같은 프로세스·같은 이벤트 루프**에서 돈다. 이게 이 방식이 싼 이유다 —
애드온이 붙든 요청과 사람이 누르는 판단이 같은 메모리 안의 Future 하나로 이어져서
IPC 도 폴링도 없다(decision.py 참고).

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


async def start() -> bool:
    """프록시를 띄운다. 띄웠으면 True.

    실패(미설치·포트 충돌)는 예외로 올리지 않는다. 프록시는 부가 기능이고
    엔진 기동을 막을 이유가 없다.
    """
    global _task, _master
    if not config.PROXY_ENABLED:
        return False
    if _task is not None:
        return True
    try:
        from mitmproxy.addons import default_addons, script
        from mitmproxy.master import Master
        from mitmproxy.options import Options

        from app.adapters.proxy.addon import CampfireAddon
    except ImportError:
        logger.info('[proxy] mitmproxy 미설치 — 프록시 비활성. pip install -e ".[proxy]"')
        return False

    try:
        opts = Options(listen_host="127.0.0.1", listen_port=config.PROXY_PORT)
        _master = Master(opts, event_loop=asyncio.get_running_loop())
        _master.addons.add(*default_addons())
        # stream_large_bodies 는 기본 애드온이 등록하는 옵션이라 Options() 생성 시점엔
        # 아직 없다(mitmproxy 11 에서 KeyError). 애드온을 올린 뒤에 명시한다.
        #
        # None = 스트리밍 안 함. 본문을 통째로 받아야 검사할 수 있다 — 스트리밍이 켜지면
        # request 훅이 본문 도착 전에 불려서 빈 바이트를 검사하게 된다. 기본값도 None
        # 이지만, 조용히 바뀌면 게이트웨이가 빈 검사를 통과시키므로 못 박아 둔다.
        opts.update(stream_large_bodies=None)
        _master.addons.add(CampfireAddon())
        _task = asyncio.create_task(_master.run())
        logger.info("[proxy] 기동 — 127.0.0.1:%s", config.PROXY_PORT)
        return True
    except Exception:
        logger.exception("[proxy] 기동 실패 — 프록시 없이 계속한다")
        _task = None
        _master = None
        return False


async def stop() -> None:
    global _task, _master
    if _master is not None:
        _master.shutdown()
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None
    _master = None
