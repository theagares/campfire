"""
app/core/detectors/registry.py
설정에서 활성 detector 를 선택한다 (PLAN §5).

v1: pii: rule_based, injection: rule_based.
교체(Phase 6): pii: encoder / injection: llm_mcp 스텁 등록 완료 — 설정값만
바꾸면(config.PII_DETECTOR / config.INJECTION_DETECTOR) registry 가 전환한다.
"""

from __future__ import annotations

from typing import Any

from app import config

from .base import Detector
from .injection import llm_mcp as injection_llm_mcp
from .injection import rule_based as injection_rule_based
from .pii import encoder as pii_encoder
from .pii import rule_based as pii_rule_based

# 이름 → builder.
_PII_BUILDERS = {
    "rule_based": pii_rule_based.build,
    "encoder": pii_encoder.build,
}
_INJECTION_BUILDERS = {
    "rule_based": injection_rule_based.build,
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
    """캐시된 detector 인스턴스를 비운다 — 설정 전환 테스트/재로드용(PLAN §5, §10 Phase 6)."""
    global _pii_detector, _injection_detector
    _pii_detector = None
    _injection_detector = None


def residency_status() -> dict[str, Any]:
    """GPU 상주 정책 상태 조회 (PLAN §4.1). /health, get_status(MCP) 에서 사용.

    rule_based detector 는 GPU 상주 개념이 없으므로(v1, 항상 즉시 사용 가능)
    residency 속성이 없다 — 그 경우 mode: "n/a" 로 표시한다.
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
