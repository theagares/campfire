"""
app/core/detectors/registry.py
활성 detector 를 들고 있는다 (PLAN §5).

**설정으로 고르지 않는다.** 룰베이스 폴백을 없앤 뒤 종류별 구현이 하나씩뿐이라
이름→빌더 조회표를 걷어냈는데, 그때 config.PII_DETECTOR / INJECTION_DETECTOR 만
남아 "고를 수 있다" 는 인상을 계속 줬다. 실제로는 아무도 그 값을 읽지 않는다 —
데스크탑이 spawn env 로 실어 보내고 값이 바뀌면 엔진을 재시작까지 했지만 결과는
언제나 같았다. 그래서 그 두 키를 config.py 에서 지웠다. 종류가 늘면 조회표를
되살리는 게 맞지, 읽지 않는 설정을 남겨두는 게 아니다.

가중치 미준비 시 미검사 통과는 여기가 아니라 파이프라인의
app.core.model_status 게이트가 처리한다.
"""

from __future__ import annotations

import contextlib
from typing import Any

from .base import Detector
from .injection import llm_mcp as injection_llm_mcp
from .pii import encoder as pii_encoder

# 이름→빌더 조회표는 없앴다. 룰베이스 폴백이 사라진 뒤로 종류별 구현이 하나씩뿐이라,
# dict 도 _build() 의 이름 검증도 존재하지 않는 대안을 위한 장치였다. 종류가 늘면
# 그때 되살리면 된다.
_pii_detector: Detector | None = None
_injection_detector: Detector | None = None


def load_detectors() -> None:
    """lifespan 기동 시 1회 호출."""
    get_pii_detector()
    get_injection_detector()


def get_pii_detector() -> Detector:
    global _pii_detector
    if _pii_detector is None:
        _pii_detector = pii_encoder.build()
    return _pii_detector


def get_injection_detector() -> Detector:
    global _injection_detector
    if _injection_detector is None:
        _injection_detector = injection_llm_mcp.build()
    return _injection_detector


def active_detectors() -> dict[str, str]:
    """실제로 돌고 있는 detector 이름. /health 와 MCP get_status 가 이걸 보여준다.

    예전 폴백은 config 값이었다 — 이름이 없는 detector 가 오면 **환경변수에 적힌
    문자열**을 활성 detector 라고 보고하게 된다. 그 값은 아무도 읽지 않으므로
    실제로 도는 것과 무관하고, 보안 도구에서 "무엇이 검사 중인가" 를 틀리게
    말하는 건 모르는 것보다 나쁘다. 모르면 모른다고 한다.
    """
    return {
        "pii": getattr(get_pii_detector(), "name", "unknown"),
        "injection": getattr(get_injection_detector(), "name", "unknown"),
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
