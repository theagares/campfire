"""VARCO 사이드카의 장치 선택·저메모리 안전 묶음 — 실제 4.3GB 가중치 없이 검증."""

from __future__ import annotations

from types import SimpleNamespace

from app.models.varco_ocr.runtime import varco_ocr_inference as runtime


class _Backend:
    def __init__(self, available: bool):
        self.available = available

    def is_available(self) -> bool:
        return self.available


def test_장치_선택은_CUDA_XPU_MPS_순서다(monkeypatch):
    monkeypatch.setattr(runtime.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(runtime.torch, "xpu", _Backend(True), raising=False)
    monkeypatch.setattr(runtime.torch.backends, "mps", _Backend(True), raising=False)
    assert runtime.resolve_device() == "cuda"

    monkeypatch.setattr(runtime.torch.cuda, "is_available", lambda: False)
    assert runtime.resolve_device() == "xpu"

    monkeypatch.setattr(runtime.torch, "xpu", _Backend(False), raising=False)
    assert runtime.resolve_device() == "mps"

    monkeypatch.setattr(runtime.torch.backends, "mps", _Backend(False), raising=False)
    assert runtime.resolve_device() is None


def test_메모리가_작은_Arc는_모델_출력조건을_안바꾸고_묶음만_줄인다(monkeypatch):
    xpu = SimpleNamespace(get_device_properties=lambda _index: SimpleNamespace(total_memory=8 * 1024**3))
    monkeypatch.setattr(runtime.torch, "xpu", xpu, raising=False)
    assert runtime._memory_safe_limits("xpu", 8, 6000) == (2, 1500)

    xpu.get_device_properties = lambda _index: SimpleNamespace(total_memory=12 * 1024**3)
    assert runtime._memory_safe_limits("xpu", 8, 6000) == (4, 3000)

    xpu.get_device_properties = lambda _index: SimpleNamespace(total_memory=16 * 1024**3)
    assert runtime._memory_safe_limits("xpu", 8, 6000) == (8, 6000)
