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

import importlib.util
import platform
import shutil
import subprocess
import sys
from functools import lru_cache

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
    """선택된 가속기에서 VARCO 사이드카를 띄울 실행 환경이 준비됐는가.

    Apple Silicon은 앱에 번들된 arm64 torch/torchvision의 MPS를 그대로 쓴다. Windows의
    CUDA/XPU는 앱 본체의 CPU torch를 바꾸지 않고 모델 받기가 별도 폴더에 설치한다.
    """
    if config.IMAGE_OCR_PYTHON_OVERRIDE:
        return True
    accelerator = image_ocr_accelerator()
    if accelerator == "mps":
        return importlib.util.find_spec("torch") is not None and importlib.util.find_spec("torchvision") is not None
    if not (config.IMAGE_OCR_GPU_TORCH_DIR / "torch" / "__init__.py").is_file():
        return False
    marker = config.IMAGE_OCR_GPU_TORCH_DIR / "accelerator.txt"
    if marker.is_file():
        try:
            return marker.read_text(encoding="ascii").strip() == accelerator
        except OSError:
            return False
    # v0.2.50이 설치한 기존 폴더에는 표식이 없다. 당시 유일한 별도 런타임은 CUDA였다.
    return accelerator == "cuda"


@lru_cache(maxsize=1)
def _intel_arc_present() -> bool:
    """Windows 디스플레이 장치 이름으로 Intel Arc를 찾는다.

    앱의 기본 torch는 CPU 빌드라 XPU 런타임을 받기 전에는 ``torch.xpu``로 하드웨어를
    찾을 수 없다. CIM은 별도 런타임 없이 Windows가 알고 있는 실제 어댑터를 돌려준다.
    """
    if sys.platform != "win32":
        return False
    try:
        proc = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                "(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue).Name",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    names = proc.stdout.casefold()
    return proc.returncode == 0 and "intel" in names and "arc" in names


def _mps_available() -> bool:
    if sys.platform != "darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
        return False
    try:
        import torch
    except (ImportError, OSError):
        return False
    backend = getattr(torch.backends, "mps", None)
    return bool(backend and backend.is_available())


def image_ocr_accelerator() -> str | None:
    """VARCO가 사용할 가속기. 우선순위는 NVIDIA CUDA → Intel XPU → Apple MPS다."""
    if config.IMAGE_OCR_PYTHON_OVERRIDE:
        return "external"
    if sys.platform == "win32":
        if shutil.which("nvidia-smi") is not None:
            return "cuda"
        if _intel_arc_present():
            return "xpu"
    if _mps_available():
        return "mps"
    return None


def image_ocr_gpu() -> bool:
    """지원 GPU(CUDA/XPU/MPS)를 쓸 수 있을 법한 PC인가. 기존 API 필드 이름은 호환을 위해 유지한다."""
    return image_ocr_accelerator() is not None


def image_ocr_ready() -> bool:
    """이미지 속 글자 검사 모델 + GPU 실행 환경. all_ready() 에 넣지 않는다 — 선택 기능이라 없다고 PII·인젝션까지
    꺼지면 안 된다. 없으면 글자 검사 대상 이미지가 든 입력은 막는다(core/detectors/image_ocr.py)."""
    return image_ocr_gpu() and image_ocr_weights_ready() and image_ocr_runtime_ready()
