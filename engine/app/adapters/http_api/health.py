"""
app/adapters/http_api/health.py
GET /health — 시그니처 필드 필수 (PLAN §11).

익스텐션이 포트 스캔 시 service == "securedoc-gateway" 로 우리 엔진을 식별한다.
"""

from __future__ import annotations

from fastapi import APIRouter

from app import config
from app.core.detectors import registry

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "service": config.SERVICE_NAME,       # 고정 시그니처 (PLAN §11)
        "port": config.BOUND_PORT,            # 실제 바인딩 포트
        "status": "ok",
        "version": "0.1.0",
        "detectors": registry.active_detectors(),
        "injectionPolicy": config.INJECTION_POLICY,
        "supportedExtensions": sorted(config.SUPPORTED_EXTENSIONS),
        "maxUploadBytes": config.MAX_UPLOAD_BYTES,
        "maxPromptChars": config.MAX_PROMPT_CHARS,
    }
