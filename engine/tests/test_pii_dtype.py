"""PII 런타임의 FP16 가속 및 CPU 안전 폴백."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import torch

_RUNTIME_DIR = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "models"
    / "pii_engine"
    / "runtime"
)


def _detector_cls():
    sys.path.insert(0, str(_RUNTIME_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "_pii_runtime_dtype", _RUNTIME_DIR / "local_pii_inference.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.PIIDetector
    finally:
        sys.path.remove(str(_RUNTIME_DIR))


PIIDetector = _detector_cls()


def test_cuda_uses_requested_fp16():
    assert PIIDetector._resolve_dtype("float16", "cuda") is torch.float16


def test_cpu_falls_back_to_fp32():
    assert PIIDetector._resolve_dtype("float16", "cpu") is torch.float32


def test_invalid_dtype_is_rejected():
    with pytest.raises(ValueError, match="Unsupported PII dtype"):
        PIIDetector._resolve_dtype("bfloat16", "cuda")


def test_mps_stays_fp32():
    # FP16 결과가 같다는 확인은 CUDA 에서만 했다 — Apple Silicon 은 기존처럼 FP32.
    assert PIIDetector._resolve_dtype("float16", "mps") is torch.float32
