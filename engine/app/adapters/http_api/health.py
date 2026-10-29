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

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, Request

from app import config
from app.adapters import proxy
from app.adapters.http_api.proxy_control import extension_route_status
from app.core import forced_mask, model_status
from app.core.detectors import image_ocr, registry

router = APIRouter()

_last_extension_seen_at: float | None = None


def _kg_file_ok() -> bool:
    return bool(config.KG_PATH) and Path(config.KG_PATH).is_file()


def _installed_extension_version() -> str | None:
    """설치 폴더 manifest의 현재 판. 교체 중이거나 손상됐으면 재로드를 유도하지 않는다."""
    if config.EXTENSION_DIR is None:
        return None
    try:
        manifest = json.loads((config.EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return version if isinstance(version, str) and version else None


@router.get("/health")
async def health(request: Request):
    global _last_extension_seen_at

    origin = request.headers.get("origin", "")
    if origin.startswith("chrome-extension://") or origin.startswith("moz-extension://"):
        _last_extension_seen_at = time.monotonic()

    extension_last_seen_seconds_ago = (
        time.monotonic() - _last_extension_seen_at if _last_extension_seen_at is not None else None
    )
    risk_scanner = getattr(request.app.state, "mcp_risk_scanner", None)
    risk_scanner_status = risk_scanner.status() if risk_scanner is not None else {
        "contractVersion": 1,
        "enabled": False,
        "state": "unavailable",
        "transport": "stdio-mcp",
        "blocksTargetUse": False,
        "scoreAffectsAppBehavior": False,
    }
    # KG 는 사용자가 고른 경로다. macOS 는 ~/Desktop·문서·다운로드 안 파일을 처음 건드리면 접근 승인
    # 창을 띄우고, 답할 때까지 그 호출이 멈춘다. 이벤트 루프에서 부르면 엔진 전체가 멈춰 확장 검사도
    # 끝없이 기다렸다(v0.2.56 맥 실측: 판 교체·키체인 승인 직후 확장이 5분 넘게 "검사 준비 중").
    kg_ok = await asyncio.to_thread(_kg_file_ok)

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
        "edition": "lite" if config.LITE else "full",  # 라이트 판: 기밀·프록시·MCP·시연 대시보드 꺼짐
        # 기밀 탐지(선택 기능) — 앱이 켜져 있을 때만 홈·대시보드·처리현황에 보여 준다.
        # KG 파일이 없으면 탐지는 아무것도 안 하므로 가중치가 있어도 작동 중이 아니다.
        "confidential": {
            "enabled": config.CONFIDENTIAL_ENABLED,
            "ready": model_status.confidential_ready() and kg_ok,
            "kgReady": kg_ok,
        },
        # 이미지 속 글자 검사(선택 기능) — noGpu 는 사이드카를 한 번 띄워 봐야 안다(None = 아직 모름)
        "imageOcr": image_ocr.status(),
        # 시연 전용 기밀 판정 근거 대시보드 경로 (SECUREDOC_DEMO_TRACE=1 일 때만) — 포트가 매번 달라 여기서 찾는다
        "demoDashboard": "/demo/dashboard" if config.DEMO_TRACE_ENABLED else None,
        "injectionPolicy": config.INJECTION_POLICY,
        "supportedExtensions": sorted(config.SUPPORTED_EXTENSIONS),
        "maxUploadBytes": config.MAX_UPLOAD_BYTES,
        "maxPromptChars": config.MAX_PROMPT_CHARS,
        "extensionLastSeenSecondsAgo": extension_last_seen_seconds_ago,
        # Chrome의 runtime.getURL(manifest.json)은 실행 중인 옛 패키지 스냅샷을 돌려줄 수 있다.
        # 확장은 이 디스크 판과 runtime.getManifest().version을 비교해 업데이트 뒤 스스로 재로드한다.
        "installedExtensionVersion": _installed_extension_version(),
        # 브라우저가 실제 응답 표식을 능동 확인해 보고한 보호 경로다. 엔진 프로세스나
        # 설정값이 아니라 실제 탭 기준이라 프록시/확장 전환 완료 여부를 확인할 수 있다.
        "extensionRoute": extension_route_status(),
        "mcpRiskScanner": risk_scanner_status,
        # 단어 원문은 절대 반환하지 않는다. 데스크탑은 count/revision 으로 동기화만 확인한다.
        "forcedMask": forced_mask.registry.status(),
        # 앱은 이 값으로 시스템 프록시를 걸지 말지 정한다. 저장된 설정이 아니라
        # **지금 실제로 듣고 있는지**를 봐야 죽은 포트로 브라우저를 돌리지 않는다.
        "proxy": proxy.status(),
    }
