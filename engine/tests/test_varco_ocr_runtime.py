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


GIB = 1024**3
CAP = (16, 18000)  # 엔진이 넘기는 상한(config IMAGE_OCR_BATCH·IMAGE_OCR_TOKEN_BUDGET)
WEIGHTS_AND_CONTEXT = 4.6 * GIB  # 가중치 약 4.1GB + CUDA 컨텍스트 약 0.5GB — 사이드카가 묶음을 정할 때 이미 올라가 있다


def _old_tier(total: float) -> tuple[int, int]:
    """예전 단계 규칙(전체 용량 기준) — 새 규칙이 빈 카드에서 이보다 작아지면 안 된다."""
    return (2, 1500) if total < 10 * GIB else (4, 3000) if total < 16 * GIB else (8, 6000)


def _cuda(monkeypatch, free: float) -> None:
    monkeypatch.setattr(runtime.torch.cuda, "mem_get_info", lambda *_a: (int(free), 24 * GIB))


def test_CUDA_는_남은_VRAM으로_묶음을_정한다(monkeypatch):
    # 12GB 빈 카드 — 가중치를 올린 뒤 약 7.4GB 가 남는다: 그림 한 장의 조각을 한 번에(예전 단계 규칙은 4/3000).
    _cuda(monkeypatch, 12 * GIB - WEIGHTS_AND_CONTEXT)
    assert runtime._memory_safe_limits("cuda", *CAP) == (16, 18000)
    # 다른 GPU 작업자가 4GB 를 쓰면 약 3.4GB 남음 → (3.4 − 1.5)GB / 0.25MB
    _cuda(monkeypatch, 12 * GIB - WEIGHTS_AND_CONTEXT - 4 * GIB)
    assert runtime._memory_safe_limits("cuda", *CAP) == (8, 7782)
    # v0.2.57 실측처럼 다른 프로세스가 7GB 를 쓰면 거의 안 남는다 → 가장 작은 묶음(그때 OOM 이 난 자리)
    _cuda(monkeypatch, 12 * GIB - WEIGHTS_AND_CONTEXT - 7 * GIB)
    assert runtime._memory_safe_limits("cuda", *CAP) == (2, 1500)
    # 상한은 넘지 않는다
    _cuda(monkeypatch, 24 * GIB - WEIGHTS_AND_CONTEXT)
    assert runtime._memory_safe_limits("cuda", 8, 6000) == (8, 6000)


def test_빈_카드에서는_예전_단계_값보다_작아지지_않는다(monkeypatch):
    for total in (8, 12, 16, 24):
        _cuda(monkeypatch, total * GIB - WEIGHTS_AND_CONTEXT)
        batch, budget = runtime._memory_safe_limits("cuda", *CAP)
        old_batch, old_budget = _old_tier(total * GIB - WEIGHTS_AND_CONTEXT + 4.1 * GIB)
        assert batch >= old_batch and budget >= old_budget, (total, batch, budget)
    _cuda(monkeypatch, 12 * GIB - WEIGHTS_AND_CONTEXT)
    assert runtime._memory_safe_limits("cuda", *CAP) > (4, 3000), "12GB 빈 카드가 예전 4/3000 보다 크지 않다"


def test_메모리를_모르면_예전_기본값으로(monkeypatch):
    def broken(*_a):
        raise RuntimeError("no cuda")

    monkeypatch.setattr(runtime.torch.cuda, "mem_get_info", broken)
    assert runtime._memory_safe_limits("cuda", *CAP) == (8, 6000)


def test_Apple_MPS_는_권장_한도에서_쓴_만큼을_빼고_정한다(monkeypatch):
    monkeypatch.setattr(runtime.torch.mps, "current_allocated_memory", lambda: int(4.3 * GIB))
    monkeypatch.setattr(runtime.torch.mps, "recommended_max_memory", lambda: int(10.7 * GIB))  # 16GB 맥
    assert runtime._memory_safe_limits("mps", *CAP) == (16, 18000)
    monkeypatch.setattr(runtime.torch.mps, "recommended_max_memory", lambda: int(5.3 * GIB))  # 8GB 맥
    assert runtime._memory_safe_limits("mps", *CAP) == (2, 1500)


def test_Intel_XPU_는_전체에서_쓴_만큼을_빼고_정한다(monkeypatch):
    xpu = SimpleNamespace(get_device_properties=lambda _index: SimpleNamespace(total_memory=8 * GIB),
                          memory_allocated=lambda *_a: int(4.3 * GIB))
    monkeypatch.setattr(runtime.torch, "xpu", xpu, raising=False)
    assert runtime._memory_safe_limits("xpu", *CAP) == (8, 9011)  # 8GB Arc — 예전 단계 규칙은 2/1500
    xpu.get_device_properties = lambda _index: SimpleNamespace(total_memory=16 * GIB)
    assert runtime._memory_safe_limits("xpu", *CAP) == (16, 18000)
