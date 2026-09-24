"""프록시를 실행 중에 켜고 끈다 — 토글이 성립하는지의 전제.

켰다 끄고 다시 켤 때 **같은 포트를 다시 잡을 수 있어야** 토글이 된다. 못 잡으면
두 번째 켜기부터 실패하거나, 더 나쁘게는 "켜짐" 이라고 답하면서 듣지 않는다 —
그 상태로 시스템 프록시를 돌리면 브라우저가 죽은 포트를 향한다.

실제 mitmproxy 를 띄운다. 라이브 엔진(48210)과 겹치지 않게 48219 를 쓴다.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

pytest.importorskip("mitmproxy")

from app import config  # noqa: E402
from app.adapters import proxy  # noqa: E402

PORT = 48219


def _bound(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(autouse=True)
def _port(monkeypatch):
    monkeypatch.setattr(config, "PROXY_PORT", PORT)
    monkeypatch.setattr(config, "PROXY_ENABLED", False)


def test_껐다_켰다_껐다_켰다():
    async def go():
        assert not proxy.is_running()

        assert await proxy.start(force=True) is True
        assert proxy.is_running()
        assert _bound(PORT)

        await proxy.stop()
        assert not proxy.is_running()
        assert not _bound(PORT), "껐는데 포트가 남아 있다"

        # 핵심: 같은 포트를 다시 잡는다.
        assert await proxy.start(force=True) is True, "두 번째 켜기 실패 — 포트 재사용 불가"
        assert proxy.is_running()
        assert _bound(PORT)

        await proxy.stop()
        assert not _bound(PORT)

    asyncio.run(go())


def test_force_없이는_설정을_따른다():
    """기동 시 자동 시작 경로. PROXY_ENABLED 가 꺼져 있으면 띄우지 않는다."""

    async def go():
        assert await proxy.start() is False
        assert not proxy.is_running()

    asyncio.run(go())


def test_포트를_못_잡으면_켜짐이라_하지_않는다():
    """mitmproxy 는 바인딩에 실패해도 로그만 남기고 돈다.

    그걸 "켜짐" 으로 보고하면 앱이 시스템 프록시를 죽은 포트로 돌린다.
    """

    async def go():
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", PORT))
        blocker.listen(1)
        try:
            assert await proxy.start(force=True) is False
            assert not proxy.is_running()
        finally:
            blocker.close()
            await proxy.stop()

    asyncio.run(go())


def test_끄면_대기중이던_판단은_차단으로_끝난다():
    """루프가 내려간 뒤 브라우저 요청이 타임아웃까지 매달리면 안 된다."""
    from app.adapters.proxy.decision import DecisionTimeout, broker

    async def go():
        waiter = asyncio.create_task(
            broker.wait(file_name="a.txt", host="claude.ai",
                        result={"stats": {}}, timeout_s=30)
        )
        await asyncio.sleep(0)
        assert len(broker.list_pending()) == 1

        await proxy.stop()

        with pytest.raises(DecisionTimeout):
            await waiter
        assert broker.list_pending() == []

    asyncio.run(go())
