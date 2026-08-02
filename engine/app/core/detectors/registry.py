"""
app/core/detectors/registry.py
설정에서 활성 detector 를 선택한다 (PLAN §5).

룰베이스 폴백은 제거했다 — pii: encoder, injection: llm_mcp 만 남는다(설정값은
config.PII_DETECTOR / config.INJECTION_DETECTOR). 가중치 미준비 시 미검사 통과는
여기가 아니라 파이프라인의 app.core.model_status 게이트가 처리한다.
"""

from __future__ import annotations

import contextlib
from typing import Any

from app import config

from .base import Detector
from .injection import llm_mcp as injection_llm_mcp
from .pii import encoder as pii_encoder

# 이름 → builder.
_PII_BUILDERS = {
    "encoder": pii_encoder.build,
}
_INJECTION_BUILDERS = {
    "llm_mcp": injection_llm_mcp.build,
}

_pii_detector: Detector | None = None
_injection_detector: Detector | None = None


def _build(kind: str, name: str) -> Detector:
    builders = _PII_BUILDERS if kind == "pii" else _INJECTION_BUILDERS
    if name not in builders:
        available = ", ".join(sorted(builders))
        raise ValueError(f"알 수 없는 {kind} detector: '{name}' (사용 가능: {available})")
    return builders[name]()


def load_detectors() -> None:
    """lifespan 기동 시 1회 호출."""
    global _pii_detector, _injection_detector
    _pii_detector = _build("pii", config.PII_DETECTOR)
    _injection_detector = _build("injection", config.INJECTION_DETECTOR)


def get_pii_detector() -> Detector:
    global _pii_detector
    if _pii_detector is None:
        _pii_detector = _build("pii", config.PII_DETECTOR)
    return _pii_detector


def get_injection_detector() -> Detector:
    global _injection_detector
    if _injection_detector is None:
        _injection_detector = _build("injection", config.INJECTION_DETECTOR)
    return _injection_detector


def active_detectors() -> dict[str, str]:
    return {
        "pii": getattr(get_pii_detector(), "name", config.PII_DETECTOR),
        "injection": getattr(get_injection_detector(), "name", config.INJECTION_DETECTOR),
    }


def reset_cache() -> None:
    """캐시된 detector 인스턴스를 비운다 — 설정 전환 테스트/재로드용(PLAN §5, §10 Phase 6).

    encoder/llm_mcp 는 실제 GPU 서브프로세스를 스폰한다 — 참조만 비우고 그 프로세스를
    종료하지 않으면, 실 모델을 반복 로드하는 테스트들이 이어서 돌 때 이전 프로세스가
    GPU 메모리를 계속 점유해 다음 로드가 자원 부족으로 실패하는 문제가 실측됐다.
    캐시를 비우기 전에 살아있는 서브프로세스가 있으면 종료 신호를 보낸다
    (Process.terminate() 자체는 동기 호출이라 이벤트 루프 없이도 안전하게 부를 수 있다 —
    종료 완료까지 기다리지는 않는 best-effort).
    """
    global _pii_detector, _injection_detector
    for det in (_pii_detector, _injection_detector):
        proc = getattr(det, "_process", None)
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.terminate()
    _pii_detector = None
    _injection_detector = None


def residency_status() -> dict[str, Any]:
    """GPU 상주 정책 상태 조회 (PLAN §4.1). /health, get_status(MCP) 에서 사용.

    residency 속성이 없는 detector 는 mode: "n/a" 로 표시한다(현재는 encoder/llm_mcp
    모두 GpuResidency 를 갖고 있어 실제로 여기 걸릴 일은 없지만, 새 detector 가
    추가돼도 안전하게 동작하도록 남겨둔다).
    """

    def _status(detector: Detector) -> dict[str, Any]:
        residency = getattr(detector, "residency", None)
        if residency is None:
            return {
                "model": getattr(detector, "name", None),
                "mode": "n/a",
                "state": "n/a",
                "idleTimeoutSec": None,
                "idleForSec": None,
                "loadCount": 0,
            }
        return residency.status

    return {
        "pii": _status(get_pii_detector()),
        "injection": _status(get_injection_detector()),
    }
