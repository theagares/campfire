"""통합 GPU worker(PII·인젝션)도 이미지 검사처럼 Windows CUDA torch(gpu_torch)를 앞세워 띄운다 — 모델 없이 검증.

v0.2.62 Windows(RTX 5070): 엔진 venv torch 가 CPU 판이라 worker 가 "CUDA 없음" 으로 끝나 이번 실행 동안 꺼지고, PII·인젝션이
CPU 사이드카로 돌았다(nvidia-smi 에 OCR 만). 이미지 검사 사이드카는 gpu_torch 를 PYTHONPATH 앞에 세워 CUDA 로 돈다.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from app import config
from app.core import model_status
from app.core.detectors import combined_gpu


@pytest.fixture
def cuda_pc(monkeypatch, tmp_path):
    """gpu_torch(CUDA) 가 깔린 Windows NVIDIA PC 를 흉내 낸다."""
    gpu_torch = tmp_path / "gpu_torch"
    monkeypatch.setattr(config, "IMAGE_OCR_GPU_TORCH_DIR", gpu_torch)
    monkeypatch.setattr(config, "IMAGE_OCR_PYTHON_OVERRIDE", "")
    monkeypatch.setattr(config, "COMBINED_GPU_PYTHON_EXECUTABLE", sys.executable)
    monkeypatch.setattr(model_status, "image_ocr_accelerator", lambda: "cuda")
    monkeypatch.setattr(model_status, "image_ocr_runtime_ready", lambda: True)
    return gpu_torch


def test_CUDA_torch_가_깔린_PC_면_그_폴더를_쓴다(cuda_pc, monkeypatch):
    assert combined_gpu.cuda_torch_path() == str(cuda_pc)
    monkeypatch.setattr(model_status, "image_ocr_runtime_ready", lambda: False)  # 아직 안 받음
    assert combined_gpu.cuda_torch_path() is None


@pytest.mark.parametrize("change", ["xpu", "mps", "no_gpu", "own_python", "ocr_override"])
def test_CUDA_torch_를_쓰지_않는_경우(cuda_pc, monkeypatch, change):
    if change in ("xpu", "mps"):
        monkeypatch.setattr(model_status, "image_ocr_accelerator", lambda: change)
    elif change == "no_gpu":
        monkeypatch.setattr(model_status, "image_ocr_accelerator", lambda: None)
    elif change == "own_python":  # SECUREDOC_COMBINED_GPU_PYTHON_EXECUTABLE 로 CUDA torch 파이썬을 직접 지정했다
        monkeypatch.setattr(config, "COMBINED_GPU_PYTHON_EXECUTABLE", r"C:\cuda\python.exe")
    else:
        monkeypatch.setattr(config, "IMAGE_OCR_PYTHON_OVERRIDE", r"C:\cuda\python.exe")
    assert combined_gpu.cuda_torch_path() is None


class _Stop(Exception):
    pass


def _spawn_env(monkeypatch) -> dict[str, str]:
    """_spawn 이 하위 프로세스에 넘기는 env 를 잡는다(실제로 띄우지 않는다)."""
    seen: dict[str, str] = {}

    async def fake_exec(*_args, env=None, **_kw):
        seen.update(env or {})
        raise _Stop

    monkeypatch.setattr(combined_gpu.backbone, "is_cached", lambda: False)
    monkeypatch.setattr(combined_gpu.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(_Stop):
        asyncio.run(combined_gpu.CombinedGpuClient()._spawn())
    return seen


def test_통합_worker_는_gpu_torch_를_PYTHONPATH_앞에_세워_띄운다(cuda_pc, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", r"C:\existing")
    env = _spawn_env(monkeypatch)
    assert env["PYTHONPATH"].split(os.pathsep) == [str(cuda_pc), r"C:\existing"]


def test_gpu_torch_가_없으면_PYTHONPATH_는_그대로(cuda_pc, monkeypatch):
    monkeypatch.setattr(model_status, "image_ocr_runtime_ready", lambda: False)
    monkeypatch.setenv("PYTHONPATH", r"C:\existing")
    env = _spawn_env(monkeypatch)
    assert env["PYTHONPATH"] == r"C:\existing"
