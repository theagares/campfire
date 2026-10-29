"""VARCO 사이드카의 장치 선택·저메모리 안전 묶음 — 실제 4.3GB 가중치 없이 검증."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.varco_ocr.runtime import varco_ocr_inference as runtime


@pytest.fixture(autouse=True)
def _no_commit_limit(monkeypatch):
    """이 PC 의 실제 남은 커밋에 시험이 흔들리지 않게 기본은 '모름'(Windows 가 아닌 것처럼)."""
    monkeypatch.setattr(runtime, "_free_commit", lambda: None)


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
    # 다른 GPU 작업자가 4GB 를 쓰면 약 3.4GB 남음 → (3.4 − 1.0 여유)GB / 0.25MB
    _cuda(monkeypatch, 12 * GIB - WEIGHTS_AND_CONTEXT - 4 * GIB)
    assert runtime._memory_safe_limits("cuda", *CAP) == (8, 9830)
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
    assert runtime._memory_safe_limits("xpu", *CAP) == (8, 11059)  # 8GB Arc — 예전 단계 규칙은 2/1500
    xpu.get_device_properties = lambda _index: SimpleNamespace(total_memory=16 * GIB)
    assert runtime._memory_safe_limits("xpu", *CAP) == (16, 18000)


def test_Windows_는_묶음_메모리를_남은_시스템_커밋으로도_묶는다(monkeypatch):
    # v0.2.62 측정: VRAM +4.8GB 일 때 사이드카 커밋 8GB — VRAM 이 넉넉해도 PC 커밋이 모자라면 큰 묶음을 쓰지 않는다.
    _cuda(monkeypatch, 12 * GIB - WEIGHTS_AND_CONTEXT)  # VRAM 은 약 7.4GB 남음
    monkeypatch.setattr(runtime, "_free_commit", lambda: 5 * GIB)  # 남은 커밋 5GB → 5 − 2(남김) = 3GB
    assert runtime._memory_safe_limits("cuda", *CAP) == (8, 8192)
    monkeypatch.setattr(runtime, "_free_commit", lambda: 2.5 * GIB)
    assert runtime._memory_safe_limits("cuda", *CAP) == (2, 1500)
    monkeypatch.setattr(runtime, "_free_commit", lambda: 40 * GIB)  # 커밋이 넉넉하면 VRAM 기준 그대로
    assert runtime._memory_safe_limits("cuda", *CAP) == (16, 18000)


def test_불러온_뒤_캐시를_비우고_나서_남은_메모리로_묶음을_정한다(monkeypatch):
    # 불러오며 버린 lm_head 자리(약 0.6GB)가 할당기 캐시에 남으면 mem_get_info 가 쓰는 중으로 본다 — 12GB 카드에 통합 worker 가
    # 같이 있으면 묶음이 4 → 2 로 줄어 그림 44장이 455 → 705초(서버 흉내 측정).
    import transformers

    order = []
    proc = SimpleNamespace(tokenizer=SimpleNamespace(padding_side="right", pad_token_id=0, convert_tokens_to_ids=lambda _t: 1))
    monkeypatch.setattr(runtime, "load_model", lambda *_a: order.append("load"))
    monkeypatch.setattr(runtime, "_empty_cache", lambda _d: order.append("empty"))
    monkeypatch.setattr(runtime, "_memory_safe_limits", lambda *_a: order.append("limits") or (4, 4112))
    monkeypatch.setattr(transformers.AutoProcessor, "from_pretrained", lambda *_a, **_k: proc)
    monkeypatch.setattr(runtime.Reader, "_setup_token_count", lambda self: None)
    runtime.Reader("models", 16, 18000, "cuda")
    assert order == ["load", "empty", "limits"]


class _OomReader:
    def __init__(self, fails: int):
        self.batch, self.budget, self.fails, self.calls, self.graph = 16, 18000, fails, [], None

    def read(self, _img):
        self.calls.append((self.batch, self.budget))
        if self.fails:
            self.fails -= 1
            raise runtime.torch.cuda.OutOfMemoryError("CUDA out of memory")
        return {"text": "읽음", "tiles": [], "secs": 1.0}


def test_OOM_이면_묶음을_반으로_줄여_다시_읽는다(monkeypatch):
    # 여유분 1.0GB 는 같은 카드의 통합 worker·브라우저가 잠깐 더 쓰면 넘칠 수 있다 — 넘쳐도 그림을 막지 않고 작은 묶음으로 끝까지.
    monkeypatch.setattr(runtime, "_empty_cache", lambda _d: None)
    reader = _OomReader(fails=2)
    assert runtime._read_shrinking(reader, None, "cuda")["text"] == "읽음"
    assert reader.calls == [(16, 18000), (8, 9000), (4, 4500)]


def test_가장_작은_묶음에서도_넘치면_실패를_그대로_올린다(monkeypatch):
    monkeypatch.setattr(runtime, "_empty_cache", lambda _d: None)
    reader = _OomReader(fails=99)
    with pytest.raises(runtime.torch.cuda.OutOfMemoryError):
        runtime._read_shrinking(reader, None, "cuda")
    assert reader.calls == [(16, 18000), (8, 9000), (4, 4500), (2, 2250), (2, 1500)], "가장 작은 묶음까지 줄인 뒤 포기"


def test_줄인_묶음은_다음_그림에서_남은_메모리로_다시_정한다(monkeypatch):
    # 서버 측정: 잠깐 넘친 뒤 줄인 묶음을 그대로 두면 44장 722초, 그림마다 남은 메모리로 다시 정하면 413초(OOM 0번).
    monkeypatch.setattr(runtime, "_empty_cache", lambda _d: None)
    spare = iter([(8, 6160), (2, 1500), (8, 6160)])  # 다른 프로세스가 잠깐 더 썼다가 놓았다
    monkeypatch.setattr(runtime, "_memory_safe_limits", lambda *_a: next(spare))
    reader = _OomReader(fails=1)
    for _ in range(3):
        runtime._read_one(reader, None, "cuda", 16, 18000)
    assert reader.calls == [(8, 6160), (4, 3080), (2, 1500), (8, 6160)], "넘치면 그 그림만 줄이고, 풀리면 다시 큰 묶음으로"


class _Tiny(runtime.torch.nn.Module):
    """VARCO 와 같은 키 모양의 작은 모델 — 체크포인트(4.53 저장) 키를 5.x 키로 바꿔 읽는지 본다."""

    def __init__(self):
        super().__init__()
        nn = runtime.torch.nn
        self.model = nn.Module()
        self.model.language_model = nn.Module()
        self.model.language_model.embed_tokens = nn.Embedding(4, 3, dtype=runtime.torch.float16)
        self.model.vision_tower = nn.Linear(2, 5, bias=False, dtype=runtime.torch.float16)
        self.lm_head = nn.Linear(3, 4, bias=False, dtype=runtime.torch.float16)

    def get_input_embeddings(self):
        return self.model.language_model.embed_tokens


def test_가중치를_버퍼_하나에_차례로_읽어도_텐서마다_제_값이_들어간다(monkeypatch, tmp_path):
    import transformers
    from safetensors.torch import save_file

    torch = runtime.torch
    big = torch.arange(12, dtype=torch.float16).reshape(4, 3)  # 큰 텐서를 먼저 — 버퍼에 남은 값이 다음 텐서로 새면 안 된다
    small = -torch.arange(1, 11, dtype=torch.float16).reshape(5, 2)
    save_file({"language_model.model.embed_tokens.weight": big, "vision_tower.vision_model.weight": small},
              str(tmp_path / "model.safetensors"))
    monkeypatch.setattr(transformers.AutoConfig, "from_pretrained", lambda *_a, **_k: None)
    monkeypatch.setattr(transformers.LlavaOnevisionForConditionalGeneration, "_from_config",
                        classmethod(lambda cls, *_a, **_k: _Tiny()))
    model = runtime.load_model(tmp_path, "cpu")
    assert torch.equal(model.get_input_embeddings().weight, big)
    assert torch.equal(model.model.vision_tower.weight, small)
    assert model.lm_head.weight.data_ptr() == model.get_input_embeddings().weight.data_ptr()  # tie_word_embeddings

    with open(tmp_path / "model.safetensors", "r+b") as f:  # 잘린 파일은 조용히 넘어가지 않는다
        f.truncate(f.seek(0, 2) - 4)
    with pytest.raises(RuntimeError, match="잘렸다"):
        runtime.load_model(tmp_path, "cpu")


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


# ── CUDA 그래프 경로(GraphDecoder) — 모델 없이 ───────────────────────────────────────────────────────────
def _hf_stop(prompt: list[int], gen: list[int], limit: int, eos: int) -> int:
    """HF generate + _stopper 가 줄 하나를 몇 번째 생성 토큰에서 멈추는지(포함) — 한 단계씩 그대로 돌려 본다."""
    torch = runtime.torch
    stop = runtime._stopper(torch.tensor([limit]), len(prompt))[0]
    for k in range(1, len(gen) + 1):
        if gen[k - 1] == eos or bool(stop(torch.tensor([prompt + gen[:k]]), None)[0]):
            return k
    return len(gen)


@pytest.mark.parametrize("gen, limit", [
    ([5, 6, 7, 1, 9, 9], 64),  # EOS(1)
    ([5, 6] + [8] * 20, 64),  # 같은 토큰 12번
    ([5, 6, 7] + [3, 4, 2] * 15, 64),  # 3토큰 주기
    (list(range(10, 110)) + [11, 12, 13, 14, 15, 16, 17] * 20, 400),  # 96토큰 넘어 7토큰 주기
    (list(range(10, 90)), 50),  # 조각별 상한
    (list(range(10, 40)), 64),  # 안 끝남
])
def test_그래프_경로는_HF_generate_와_같은_자리에서_멈춘다(gen, limit):
    prompt = [0, 0, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110]  # 왼쪽 패딩 포함
    assert runtime._stop_at(prompt + gen, len(prompt), limit, eos=1) == _hf_stop(prompt, gen, limit, eos=1)


@pytest.mark.parametrize("error", [RuntimeError("CUDA error: an illegal memory access was encountered"),
                                   runtime.torch.cuda.OutOfMemoryError("CUDA out of memory")])
def test_그래프_경로의_오류는_무엇이든_GraphFailed_로_올린다(error):
    graph = runtime.GraphDecoder.__new__(runtime.GraphDecoder)

    def boom(*_a):
        raise error

    graph._rows = boom
    with pytest.raises(runtime.GraphFailed, match=type(error).__name__):
        graph.rows(None, None)


def test_그래프를_켠_프로세스만_CUDA_오류에_버린다():
    on, off = SimpleNamespace(graph=object()), SimpleNamespace(graph=None)
    assert runtime._fatal(on, runtime.GraphFailed("x"))
    assert runtime._fatal(on, RuntimeError("CUDA error: an illegal memory access was encountered"))  # 다른 자리에서 나와도
    assert not runtime._fatal(on, ValueError("그림을 열 수 없음"))
    assert not runtime._fatal(off, RuntimeError("CUDA error: an illegal memory access was encountered"))


class _Inputs(dict):
    def to(self, *_a):
        return self


def test_그래프가_긴_묶음을_넘기면_그_묶음만_HF_generate_로(monkeypatch):
    from PIL import Image

    torch = runtime.torch
    reader = runtime.Reader.__new__(runtime.Reader)
    reader.device, reader.batch, reader.budget, reader.eos, reader.pad = "cuda", 16, 18000, 1, 0
    reader._ntok = lambda _c: 10
    reader.proc = SimpleNamespace(
        apply_chat_template=lambda convs, **_k: _Inputs(input_ids=torch.zeros((len(convs), 4), dtype=torch.long)),
        decode=lambda ids, **_k: "읽은 글 " + "-".join(map(str, ids)))
    monkeypatch.setattr(runtime, "segment", lambda _img: [(0, (0, 0, 200, 40)), (0, (210, 0, 400, 40))])
    calls = []
    reader.model = SimpleNamespace(generate=lambda **_k: calls.append("hf") or torch.tensor([[0] * 4 + [7, 8, 1, 0]] * 2))
    reader.graph = SimpleNamespace(rows=lambda inputs, limits: [[5, 6], [5]])
    assert "5-6" in reader.read(Image.new("RGB", (400, 40)))["text"] and calls == []
    reader.graph = SimpleNamespace(rows=lambda inputs, limits: None)  # _GRAPH_NEW 를 넘겨도 안 끝남
    assert "7-8" in reader.read(Image.new("RGB", (400, 40)))["text"] and calls == ["hf"]


def test_CUDA_그래프면_묶음_크기별_그래프_몫을_빼고_묶음을_정한다(monkeypatch):
    # 서버 실측 그래프 몫(예약): 묶음 2 174MB · 4 400MB · 8 742MB · 16 1510MB → 0.1GB + 줄당 96MB.
    _cuda(monkeypatch, 12 * GIB - WEIGHTS_AND_CONTEXT)  # 12GB 에 OCR 만 — 그래프 몫을 빼도 큰 묶음
    assert runtime._memory_safe_limits("cuda", *CAP, graph=True) == (16, 18000)
    _cuda(monkeypatch, 3.4 * GIB)  # 다른 GPU 작업자가 4GB
    assert runtime._memory_safe_limits("cuda", *CAP) == (8, 9830)
    assert runtime._memory_safe_limits("cuda", *CAP, graph=True) == (8, 6348)
    _cuda(monkeypatch, 2.5 * GIB)  # 서버 12GB + 통합 worker 흉내(상한 6.5GB − 가중치)
    assert runtime._memory_safe_limits("cuda", *CAP) == (8, 6144)
    assert runtime._memory_safe_limits("cuda", *CAP, graph=True) == (4, 4198), "그래프 몫을 빼면 한 단계 작은 묶음"
