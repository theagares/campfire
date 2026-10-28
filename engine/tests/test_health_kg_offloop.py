"""/health 의 KG 파일 확인이 멈춰도 엔진의 다른 요청은 응답해야 한다.

macOS 는 ~/Desktop·문서·다운로드 안 파일을 처음 건드리면 접근 승인 창을 띄우고, 답할 때까지
그 호출이 멈춘다. 이벤트 루프에서 부르면 엔진 전체가 멈췄다(v0.2.56 맥 실측).
모델을 올리지 않도록 /health·/activity 처리기만 단 작은 앱으로 본다.
"""

import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.http_api import activity, health
from app.core.detectors import registry


def test_health_kg_check_does_not_freeze_engine(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def stuck_kg_check():
        entered.set()
        release.wait(3)  # 사용자가 승인 창에 답하기 전
        return True

    monkeypatch.setattr(health, "_kg_file_ok", stuck_kg_check)
    monkeypatch.setattr(registry, "active_detectors", lambda: {})
    app = FastAPI()
    app.include_router(health.router)
    app.include_router(activity.router)

    with TestClient(app) as client:
        pending = threading.Thread(target=lambda: client.get("/health"))
        pending.start()
        try:
            assert entered.wait(3)
            started = time.monotonic()
            assert client.get("/activity").status_code == 200
            assert time.monotonic() - started < 1.5, "KG 확인이 이벤트 루프를 막아 다른 요청까지 멈췄다"
        finally:
            release.set()
            pending.join(5)
