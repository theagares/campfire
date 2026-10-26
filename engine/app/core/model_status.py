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

import shutil
import sys

from app import config
from app.core.detectors.injection import backbone

PII_REQUIRED_FILES = (
    "config.json", "checkpoint/best.safetensors", "checkpoint/status.json",
    "assets/backbone/config.json", "assets/backbone/tokenizer.json",
    "assets/stage_a/config.json", "assets/stage_a/char_vocab.json", "assets/stage_a/train_gazetteer.json",
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


CONFIDENTIAL_REQUIRED_FILES = ("model.safetensors", "config.json", "tokenizer.json")


def confidential_ready() -> bool:
    """기밀 탐지 리트리버 가중치. all_ready() 에 넣지 않는다 — 회사 KG 를 쓰는 선택 기능이라
    없다고 PII·인젝션까지 꺼지면 안 된다. 없으면 Solar 로 보내지 않고 근거 문장을 가린다."""
    model_dir = config.CONFIDENTIAL_ENGINE_DIR / "models"
    return all((model_dir / f).is_file() for f in CONFIDENTIAL_REQUIRED_FILES)


# VARCO-VISION-2.0-1.7B (이미지 속 글자 검사) — Hugging Face 커밋을 고정해 받는 파일들(adapters/http_api/models.py)
IMAGE_OCR_FILES = (
    "model.safetensors", "config.json", "generation_config.json", "preprocessor_config.json", "processor_config.json",
    "video_preprocessor_config.json", "chat_template.jinja", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "added_tokens.json", "vocab.json", "merges.txt",
)


def image_ocr_weights_ready() -> bool:
    model_dir = config.IMAGE_OCR_ENGINE_DIR / "models"
    return all((model_dir / f).is_file() for f in IMAGE_OCR_FILES)


def image_ocr_runtime_ready() -> bool:
    """GPU 실행 환경 — 따로 지정한 파이썬이 있거나, 모델 받기가 CUDA torch 를 깔았다(이름을 바꿔 넣어 반쯤 깔린 상태는 없다)."""
    return bool(config.IMAGE_OCR_PYTHON_OVERRIDE) or (config.IMAGE_OCR_GPU_TORCH_DIR / "torch" / "__init__.py").is_file()


def image_ocr_gpu() -> bool:
    """CUDA 를 쓸 수 있을 법한 PC 인가. 아니면 모델·실행 환경을 받지 않는다(그림이 든 입력은 막힌다)."""
    # ponytail: nvidia-smi 유무만 본다 — 드라이버가 cu130 하한(580)보다 낮거나 GPU 가 너무 오래되면 다 받은 뒤
    # 사이드카가 no_gpu 로 막는다. 드라이버 판 확인을 넣으면 받기 전에 거를 수 있다.
    return bool(config.IMAGE_OCR_PYTHON_OVERRIDE) or (sys.platform == "win32" and shutil.which("nvidia-smi") is not None)


def image_ocr_ready() -> bool:
    """이미지 속 글자 검사 모델 + GPU 실행 환경. all_ready() 에 넣지 않는다 — 선택 기능이라 없다고 PII·인젝션까지
    꺼지면 안 된다. 없으면 글자 검사 대상 이미지가 든 입력은 막는다(core/detectors/image_ocr.py)."""
    return image_ocr_weights_ready() and image_ocr_runtime_ready()
