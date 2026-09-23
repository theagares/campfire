"""
app/adapters/http_api/proxy_control.py
실행 중에 프록시를 켜고 끈다. 데스크탑 앱의 토글이 부른다.

여기서는 **엔진 쪽 프록시만** 다룬다. 브라우저가 이리로 오게 하는 시스템 프록시
설정은 앱이 만진다 — 엔진은 taskkill /F 로 강제 종료돼 스스로 설정을 되돌릴
기회가 없어서, 되돌리는 책임을 엔진보다 오래 사는 쪽에 둔다.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.adapters import proxy

router = APIRouter()


@router.get("/proxy/status")
async def proxy_status() -> dict:
    return proxy.status()


@router.post("/proxy/start")
async def proxy_start() -> dict:
    # 켰는지 여부는 start() 의 반환값이 아니라 status 로 알린다 — 앱은 늘 같은
    # 모양을 받고, "켜라고 했는데 안 켜진" 경우를 running=False 로 알아챈다.
    await proxy.start(force=True)
    return proxy.status()


@router.post("/proxy/stop")
async def proxy_stop() -> dict:
    await proxy.stop()
    return proxy.status()
