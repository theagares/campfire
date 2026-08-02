"""
app/core/model_status.py
PII/인젝션 실 모델(encoder/llm_mcp) 가중치가 로컬에 준비됐는지 확인한다.

룰베이스 폴백을 없앤 뒤에는 이 두 실 모델이 유일한 detector 구현이다. 가중치가
아직 안 받아진 상태에서 detect()를 부르면 서브프로세스가 로딩에 실패해 예외로
죽으므로, 파이프라인(app/core/pipeline/orchestrator.py)이 먼저 이 모듈로 준비
여부를 확인해 미검사 통과 여부를 판단한다. 같은 판정 로직을 HTTP API의
/models/status(app/adapters/http_api/models.py)도 그대로 재사용한다.
"""

from __future__ import annotations

from app import config
from app.core.detectors.injection import backbone

PII_REQUIRED_FILES = (
    "model.safetensors", "config.json", "label_map.json",
    "gazetteer.json", "tokenizer.json", "tokenizer_config.json",
)
INJECTION_REQUIRED_FILES = ("model.pt", "calibration.json", "norm_stats.pt")


def pii_ready() -> bool:
    seed_dir = config.PII_ENGINE_DIR / "models" / config.PII_MODEL_SEED
    return all((seed_dir / f).is_file() for f in PII_REQUIRED_FILES)


def injection_head_ready() -> bool:
    variant_dir = config.INJECTION_ENGINE_DIR / config.INJECTION_VARIANT
    return all((variant_dir / f).is_file() for f in INJECTION_REQUIRED_FILES)


def injection_ready() -> bool:
    """헤드만으론 부족하다 — 백본(EXAONE 2.4GB)이 없으면 헤드가 있어도 서브프로세스가
    로딩에 실패한다(실사용자 macOS 신규 설치에서 재현된 문제, models.py 참고)."""
    return injection_head_ready() and backbone.is_cached()


def all_ready() -> bool:
    return pii_ready() and injection_ready()
