"""/models/fetch — 기밀 탐지 리트리버는 기밀 탐지를 켠 경우에만, 없을 때만 받는다 (실제 다운로드 없이 흐름만)."""

from __future__ import annotations

import asyncio

import pytest

from app import config
from app.adapters.http_api import models
from app.core import model_status


@pytest.fixture
def fetched(monkeypatch):
    got: list[str] = []
    events: list[dict] = []

    async def fake_download(name, spec, emit):
        got.append(name)

    async def emit(ev):
        events.append(ev)

    async def fake_image_ocr(emit):
        got.append("image_ocr")

    async def fake_gpu_torch(emit, accelerator):
        got.append(f"gpu_torch:{accelerator}")

    monkeypatch.setattr(models, "_download_and_extract", fake_download)
    monkeypatch.setattr(models, "_download_image_ocr", fake_image_ocr)  # 없으면 4.3GB 를 실제로 받으러 간다
    monkeypatch.setattr(models, "_install_gpu_torch", fake_gpu_torch)  # 없으면 2GB 를 받아 깐다
    monkeypatch.setattr(models, "_pii_weights_present", lambda: True)
    monkeypatch.setattr(models, "_injection_head_present", lambda: True)
    monkeypatch.setattr(models.backbone, "is_cached", lambda: True)
    monkeypatch.setattr(models.job_registry, "make_emit", lambda _job: emit)
    return got, events


@pytest.mark.parametrize("enabled, ready, want", [(True, False, ["confidential"]), (True, True, []), (False, False, [])])
def test_기밀_리트리버는_켜져_있고_없을_때만_받는다(fetched, monkeypatch, enabled, ready, want):
    got, events = fetched
    monkeypatch.setattr(config, "CONFIDENTIAL_ENABLED", enabled)
    monkeypatch.setattr(model_status, "confidential_ready", lambda: ready)
    asyncio.run(models._fetch_job("job"))
    assert got == want
    assert events[-1]["type"] == "done"
    assert events[-1]["result"]["confidential"] == {"enabled": enabled, "ready": ready}


@pytest.mark.parametrize("enabled, accelerator, weights, runtime, want", [
    (True, "cuda", False, False, ["image_ocr", "gpu_torch:cuda"]),
    (True, "xpu", True, False, ["gpu_torch:xpu"]),
    (True, "cuda", True, True, []),
    (True, None, False, False, []),  # GPU 가 없으면 둘 다 받지 않는다 — 그림이 든 입력은 막힌다
    (False, "cuda", False, False, []),
])
def test_이미지_검사는_켜져_있고_GPU_가_있을_때_없는_것만_받는다(
    fetched, monkeypatch, enabled, accelerator, weights, runtime, want
):
    got, events = fetched
    monkeypatch.setattr(config, "CONFIDENTIAL_ENABLED", False)
    monkeypatch.setattr(config, "IMAGE_OCR_ENABLED", enabled)
    monkeypatch.setattr(model_status, "image_ocr_accelerator", lambda: accelerator)
    monkeypatch.setattr(model_status, "image_ocr_gpu", lambda: accelerator is not None)
    monkeypatch.setattr(model_status, "image_ocr_weights_ready", lambda: weights)
    monkeypatch.setattr(model_status, "image_ocr_runtime_ready", lambda: runtime)
    monkeypatch.setattr(model_status, "image_ocr_ready", lambda: bool(accelerator) and weights and runtime)
    asyncio.run(models._fetch_job("job"))
    assert got == want
    assert events[-1]["result"]["imageOcr"] == {
        "enabled": enabled,
        "ready": bool(accelerator) and weights and runtime,
        "gpu": accelerator is not None,
        "accelerator": accelerator,
    }


def test_Apple_MPS는_번들_런타임을_쓰고_별도_torch를_설치하지_않는다(fetched, monkeypatch):
    got, events = fetched
    monkeypatch.setattr(config, "CONFIDENTIAL_ENABLED", False)
    monkeypatch.setattr(config, "IMAGE_OCR_ENABLED", True)
    monkeypatch.setattr(model_status, "image_ocr_accelerator", lambda: "mps")
    monkeypatch.setattr(model_status, "image_ocr_gpu", lambda: True)
    monkeypatch.setattr(model_status, "image_ocr_weights_ready", lambda: False)
    monkeypatch.setattr(model_status, "image_ocr_runtime_ready", lambda: True)
    monkeypatch.setattr(model_status, "image_ocr_ready", lambda: False)
    asyncio.run(models._fetch_job("job"))
    assert got == ["image_ocr"]
    assert events[-1]["type"] == "done"


def test_Apple_MPS_번들_런타임이_없으면_4GB_가중치를_받기_전에_실패한다(fetched, monkeypatch):
    got, events = fetched
    monkeypatch.setattr(config, "CONFIDENTIAL_ENABLED", False)
    monkeypatch.setattr(config, "IMAGE_OCR_ENABLED", True)
    monkeypatch.setattr(model_status, "image_ocr_accelerator", lambda: "mps")
    monkeypatch.setattr(model_status, "image_ocr_runtime_ready", lambda: False)
    asyncio.run(models._fetch_job("job"))
    assert got == []
    assert events[-1]["type"] == "error" and "다시 설치" in events[-1]["message"]


def test_GPU_실행_환경은_받은_휠을_임시_폴더에_깔고_이름을_바꿔_넣는다(monkeypatch, tmp_path):
    dest = tmp_path / "gpu_torch"
    (tmp_path / "gpu_torch.partial").mkdir()  # 지난번 실패분 — pip --target 이 거부하므로 지워야 한다
    monkeypatch.setattr(config, "IMAGE_OCR_GPU_TORCH_DIR", dest)
    monkeypatch.setattr(config, "IMAGE_OCR_PYTHON_OVERRIDE", "")
    monkeypatch.setattr(model_status, "image_ocr_accelerator", lambda: "cuda")
    urls, pip_args = [], []

    async def fake_download(name, label, url, sha, path, emit):
        urls.append(url)
        path.write_bytes(b"whl")

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", None

    async def fake_exec(*args, **kwargs):
        pip_args.extend(args)
        assert args[args.index("--target") + 1] == str(tmp_path / "gpu_torch.partial")
        assert not (tmp_path / "gpu_torch.partial").exists()  # 지난번 실패분은 pip 전에 지웠다
        (tmp_path / "gpu_torch.partial" / "torch").mkdir(parents=True)
        (tmp_path / "gpu_torch.partial" / "torch" / "__init__.py").write_text("")
        return FakeProc()

    async def emit(ev):
        pass

    monkeypatch.setattr(models, "_download", fake_download)
    monkeypatch.setattr(models.asyncio, "create_subprocess_exec", fake_exec)
    assert not model_status.image_ocr_runtime_ready()
    asyncio.run(models._install_gpu_torch(emit, "cuda"))
    assert model_status.image_ocr_runtime_ready() and not (tmp_path / "gpu_torch.partial").exists()
    assert (dest / "accelerator.txt").read_text() == "cuda"
    assert all("%2Bcu130" in u for u in urls) and urls[-1].endswith("torch-2.14.1%2Bcu130-cp311-cp311-win_amd64.whl")
    assert {"--no-index", "--no-deps"} <= set(pip_args) and pip_args[-1].endswith("torch-2.14.1+cu130-cp311-cp311-win_amd64.whl")


def test_Intel_Arc_실행_환경은_XPU_휠과_공식_의존성_인덱스를_쓴다(monkeypatch, tmp_path):
    dest = tmp_path / "gpu_torch"
    monkeypatch.setattr(config, "IMAGE_OCR_GPU_TORCH_DIR", dest)
    urls, pip_args = [], []

    async def fake_download(name, label, url, sha, path, emit):
        urls.append(url)
        path.write_bytes(b"whl")

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", None

    async def fake_exec(*args, **kwargs):
        pip_args.extend(args)
        (tmp_path / "gpu_torch.partial" / "torch").mkdir(parents=True)
        (tmp_path / "gpu_torch.partial" / "torch" / "__init__.py").write_text("")
        return FakeProc()

    async def emit(ev):
        pass

    monkeypatch.setattr(models, "_download", fake_download)
    monkeypatch.setattr(models.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(models._install_gpu_torch(emit, "xpu"))
    assert (dest / "accelerator.txt").read_text() == "xpu"
    assert all("%2Bxpu" in u for u in urls)
    assert "https://download.pytorch.org/whl/xpu" in pip_args
    assert "--no-deps" not in pip_args
    assert pip_args[-1].endswith("torch-2.14.1+xpu-cp311-cp311-win_amd64.whl")


def test_설치된_GPU_런타임_종류가_현재_가속기와_다르면_다시_받는다(monkeypatch, tmp_path):
    dest = tmp_path / "gpu_torch"
    (dest / "torch").mkdir(parents=True)
    (dest / "torch" / "__init__.py").write_text("")
    (dest / "accelerator.txt").write_text("cuda")
    monkeypatch.setattr(config, "IMAGE_OCR_GPU_TORCH_DIR", dest)
    monkeypatch.setattr(config, "IMAGE_OCR_PYTHON_OVERRIDE", "")
    monkeypatch.setattr(model_status, "image_ocr_accelerator", lambda: "xpu")
    assert not model_status.image_ocr_runtime_ready()

    (dest / "accelerator.txt").write_text("xpu")
    assert model_status.image_ocr_runtime_ready()


def test_이미지_검사_모델은_커밋과_가중치_해시를_고정한다():
    assert len(models._IMAGE_OCR_REVISION) == 40 and len(models._IMAGE_OCR_WEIGHTS_SHA256) == 64
    assert "model.safetensors" in model_status.IMAGE_OCR_FILES


def test_이미지_검사_가속기는_CUDA_XPU_MPS_순서로_고른다(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_OCR_PYTHON_OVERRIDE", "")
    monkeypatch.setattr(model_status.sys, "platform", "win32")
    monkeypatch.setattr(model_status.shutil, "which", lambda _name: r"C:\\nvidia-smi.exe")
    monkeypatch.setattr(model_status, "_intel_arc_present", lambda: True)
    monkeypatch.setattr(model_status, "_mps_available", lambda: False)
    assert model_status.image_ocr_accelerator() == "cuda"

    monkeypatch.setattr(model_status.shutil, "which", lambda _name: None)
    assert model_status.image_ocr_accelerator() == "xpu"

    monkeypatch.setattr(model_status.sys, "platform", "darwin")
    monkeypatch.setattr(model_status, "_mps_available", lambda: True)
    assert model_status.image_ocr_accelerator() == "mps"


def test_Intel_Arc는_Windows_디스플레이_장치_이름으로_찾는다(monkeypatch):
    class Result:
        returncode = 0
        stdout = "Intel(R) Arc(TM) Graphics\n"

    monkeypatch.setattr(model_status.sys, "platform", "win32")
    monkeypatch.setattr(model_status.subprocess, "run", lambda *args, **kwargs: Result())
    model_status._intel_arc_present.cache_clear()
    try:
        assert model_status._intel_arc_present()
    finally:
        model_status._intel_arc_present.cache_clear()


def test_기밀_리트리버_자산은_고정된_체크섬과_위치를_쓴다():
    spec = models._ASSETS["confidential"]
    assert spec["url"].endswith("/models-v1/confidential_engine_v1.tar.gz")
    assert len(spec["sha256"]) == 64
    assert spec["extract_to"]() == config.CONFIDENTIAL_ENGINE_DIR
