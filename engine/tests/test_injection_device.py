"""인젝션 런타임의 디바이스/dtype 선택 (local_injection_hybrid_inference.py).

Apple Silicon 에서 1.2B 백본이 통째로 CPU 로 돌던 문제를 고정한다 — 예전엔 이 런타임이
cuda 만 확인하고 mps 분기가 없어서, 정작 가벼운 PII 모델만 MPS 가속을 받고 무거운 쪽이
못 받는 비대칭이었다.
"""

import importlib.util
from pathlib import Path

import pytest
import torch

_RUNTIME = (
    Path(__file__).resolve().parents[1]
    / "app" / "models" / "injection_engine" / "runtime" / "local_injection_hybrid_inference.py"
)


def _detector_cls():
    spec = importlib.util.spec_from_file_location("_inj_runtime", _RUNTIME)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return next(c for c in vars(mod).values() if isinstance(c, type) and hasattr(c, "_resolve_device"))


D = _detector_cls()


def _fake_backends(monkeypatch, *, cuda: bool, mps: bool):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: cuda)

    class _MPS:
        @staticmethod
        def is_available():
            return mps

    monkeypatch.setattr(torch.backends, "mps", _MPS, raising=False)


def test_explicit_device_is_respected(monkeypatch):
    _fake_backends(monkeypatch, cuda=True, mps=True)
    assert D._resolve_device("cpu") == "cpu"
    assert D._resolve_device("mps") == "mps"


def test_cuda_wins_when_present(monkeypatch):
    _fake_backends(monkeypatch, cuda=True, mps=True)
    assert D._resolve_device("auto") == "cuda"


def test_mps_is_used_on_apple_silicon(monkeypatch):
    """이 케이스가 이 변경의 목적 — 예전엔 여기서 'cpu' 가 나왔다."""
    _fake_backends(monkeypatch, cuda=False, mps=True)
    assert D._resolve_device("auto") == "mps"


def test_cpu_when_no_accelerator(monkeypatch):
    _fake_backends(monkeypatch, cuda=False, mps=False)
    assert D._resolve_device("auto") == "cpu"


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_dtype_untouched_off_mps(device):
    assert D._resolve_dtype("bfloat16", device) is torch.bfloat16


def test_non_bfloat16_untouched_on_mps():
    assert D._resolve_dtype("float16", "mps") is torch.float16


def test_bfloat16_falls_back_to_float16_when_mps_rejects_it(monkeypatch):
    """MPS 가 bf16 을 못 받으면 float16 으로 내린다 — 그대로 두면 로드에서 죽는다."""
    def _boom(*a, **k):
        raise RuntimeError("bfloat16 not supported on MPS")

    monkeypatch.setattr(torch, "zeros", _boom)
    assert D._resolve_dtype("bfloat16", "mps") is torch.float16
