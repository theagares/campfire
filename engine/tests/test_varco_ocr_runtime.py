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


# 조각 입력 토큰 — 묶음을 짜려고 조각마다 _encode(전처리)를 한 번 더 돌리지 않고 프로세서 식으로 센다(v0.2.62: GPU 사용률 5%).
RESOLUTIONS = [(600, 40), (1255, 319), (90, 90), (2364, 1038), (300, 1200), (50, 200), (1448, 1086), (420, 226),
               (1200, 60), (31, 400)]


class _FakeProc:
    """그림 몫 = 가로 + 세로, 프롬프트·대화 틀 몫 = 98 인 가짜 프로세서."""

    image_processor = SimpleNamespace(size={"height": 384, "width": 384})

    def __init__(self):
        self.encoded = 0

    def _get_number_of_features(self, h, w, _hh, _ww):
        return h + w

    def apply_chat_template(self, convs, **_kw):
        self.encoded += 1
        w, h = convs[0][0]["content"][0]["image"].size
        return {"input_ids": runtime.torch.zeros((1, h + w + 98))}


def test_조각_토큰은_프롬프트_몫을_한_번만_재고_식으로_센다():
    from PIL import Image

    reader = runtime.Reader.__new__(runtime.Reader)
    reader.proc = _FakeProc()
    reader._setup_token_count()
    assert reader.text_tokens == 98 and reader.proc.encoded == 1
    assert [reader._ntok(Image.new("RGB", wh)) for wh in RESOLUTIONS] == [w + h + 98 for w, h in RESOLUTIONS]
    assert reader.proc.encoded == 1, "조각마다 _encode(전처리)를 다시 돌렸다"


def test_식으로_센_토큰이_실제_프로세서_인코딩과_같다():
    """실제 VARCO 프로세서(가중치 없이)로 여러 해상도를 비교한다 — torchvision·모델 파일이 있을 때만(엔진 venv 에는 없다)."""
    import pytest
    from PIL import Image

    pytest.importorskip("torchvision")
    from app import config

    model_dir = config.IMAGE_OCR_ENGINE_DIR / "models"
    if not (model_dir / "preprocessor_config.json").exists():
        pytest.skip("VARCO 프로세서 파일이 없다")
    from transformers import AutoProcessor

    reader = runtime.Reader.__new__(runtime.Reader)
    reader.proc = AutoProcessor.from_pretrained(model_dir)
    reader.proc.tokenizer.padding_side = "left"
    reader._setup_token_count()
    for wh in RESOLUTIONS:
        crop = Image.new("RGB", wh)
        assert reader._encode([crop])["input_ids"].shape[1] == reader._ntok(crop), wh
