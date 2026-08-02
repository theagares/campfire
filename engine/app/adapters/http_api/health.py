"""
app/adapters/http_api/health.py
GET /health — 시그니처 필드 필수 (PLAN §11).

익스텐션이 포트 스캔 시 service == "campfire" 로 우리 엔진을 식별한다.

익스텐션 "연결됨" 판정(데스크탑 앱의 연결 화면, PLAN §8 — "활성 세션" 기준)에 쓰라고
마지막으로 확장에서 온 요청 시각을 같이 내려준다. 브라우저가 cross-origin fetch 에
자동으로 붙이는 Origin 헤더가 확장 background(service worker)에서는
"chrome-extension://<id>" 형태라, 별도 확장 코드 수정 없이 이 값만으로 식별 가능하다.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from app import config
from app.core import model_status
from app.core.detectors import registry

router = APIRouter()

_last_extension_seen_at: float | None = None


@router.get("/health")
async def health(request: Request):
    global _last_extension_seen_at

    origin = request.headers.get("origin", "")
    if origin.startswith("chrome-extension://") or origin.startswith("moz-extension://"):
        _last_extension_seen_at = time.monotonic()

    extension_last_seen_seconds_ago = (
        time.monotonic() - _last_extension_seen_at if _last_extension_seen_at is not None else None
    )

    return {
        "service": config.SERVICE_NAME,       # 고정 시그니처 (PLAN §11)
        "port": config.BOUND_PORT,            # 실제 바인딩 포트
        "status": "ok",
        "version": "0.1.0",
        "detectors": registry.active_detectors(),
        # 룰베이스 폴백 제거 후 detectors 이름은 항상 encoder/llm_mcp라 "탐지가 실제로
        # 동작하는지"를 이름만으로 알 수 없다 — 가중치 실제 존재 여부를 따로 내려준다
        # (데스크탑 앱의 모델 pill 이 이걸로 활성/비활성을 판단한다, ipc.js 참고).
        "modelsReady": {"pii": model_status.pii_ready(), "injection": model_status.injection_ready()},
        "injectionPolicy": config.INJECTION_POLICY,
        "supportedExtensions": sorted(config.SUPPORTED_EXTENSIONS),
        "maxUploadBytes": config.MAX_UPLOAD_BYTES,
        "maxPromptChars": config.MAX_PROMPT_CHARS,
        "extensionLastSeenSecondsAgo": extension_last_seen_seconds_ago,
    }
