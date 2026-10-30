"""Windows CUDA 에서 이미지 OCR 을 통합 GPU worker(PII·인젝션) 프로세스 안에서 읽는다 — 모델 없이 검증.

CUDA·torch 바닥(Windows 커밋 약 2GB·VRAM 약 0.4GB)을 한 번만 쓰려고 합쳤다. 대신 그림 하나가 PII·인젝션 프로세스를 끝낼 수
있어 격리를 본다: OCR 오류·죽음은 worker 실패로 세지 않고, 끝나면 다시 띄워 같은 그림을 다시 읽으며, 그동안 줄 선 탐지 요청은
새 worker 가 받는다. 그래프를 끄고도 끝나면 이번 실행 동안 사이드카로 넘긴다.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import pytest

from app import config
from app.core import model_status
from app.core.detectors import combined_gpu, image_ocr

FAKE = Path(__file__).with_name("fake_combined_worker.py")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest.fixture
def client(monkeypatch):
    """가짜 stdio worker 를 띄우는 진짜 CombinedGpuClient."""
    monkeypatch.setenv("FAKE_OCR", "1")
    c = combined_gpu.CombinedGpuClient()
    c._disabled = False
    c._args = lambda: [sys.executable, str(FAKE)]
    return c


def _run(coro_fn):
    async def main():
        return await coro_fn()
    return asyncio.run(main())


async def _pid(c) -> int:
    return (await c._request({"command": "pii", "texts": ["글"]}))["pid"]


# ── 클라이언트(combined_gpu) ─────────────────────────────────────────────────────────

def test_OCR_오류_답은_worker_를_끄지_않고_실패로_세지_않는다(client):
    async def go():
        before = await _pid(client)
        await client.ocr_load(cuda_graph=True)
        with pytest.raises(combined_gpu.CombinedGpuOcrError, match="out of memory"):
            await client.ocr_read(_b64("oom"), cuda_graph=True)
        assert client._failures == 0 and client.ocr_loaded, "그림 한 장의 오류로 PII·인젝션을 내리면 안 된다"
        assert await _pid(client) == before
        await client.stop()
    _run(go)


def test_OCR_중_답_없이_끝나면_OcrDied_로_알리고_세지_않으며_다음_요청이_다시_띄운다(client):
    async def go():
        before = await _pid(client)
        with pytest.raises(combined_gpu.CombinedGpuOcrDied):
            await client.ocr_read(_b64("die"), cuda_graph=True)
        assert client._failures == 0 and not client.ocr_loaded and client._process is None
        assert await _pid(client) != before, "탐지 요청은 새 worker 가 받아야 한다"
        await client.stop()
    _run(go)


def test_graph_failed_답이면_바로_정리해_다음_탐지에_죽어_가는_프로세스를_주지_않는다(client):
    async def go():
        before = await _pid(client)
        with pytest.raises(combined_gpu.CombinedGpuOcrError, match="graph_failed"):
            await client.ocr_read(_b64("graph"), cuda_graph=True)
        assert client._process is None and not client.ocr_loaded and client._failures == 0
        assert await _pid(client) != before
        await client.stop()
    _run(go)


def test_OCR_이_worker_를_끝내도_뒤에_줄_선_탐지_요청은_새_worker_로_받는다(client):
    async def go():
        await client.ensure_ready()
        before = client._process.pid
        ocr = asyncio.create_task(client.ocr_read(_b64("die"), cuda_graph=True))
        await asyncio.sleep(0)  # OCR 이 먼저 줄을 선다
        pid = await _pid(client)  # 예전엔 "worker was unloaded" 로 개별 sidecar 로 밀려났다
        with pytest.raises(combined_gpu.CombinedGpuOcrDied):
            await ocr
        assert pid != before and client._failures == 0
        await client.stop()
    _run(go)


def test_가중치_내리기는_worker_를_남기고_떠_있지_않으면_띄우지_않는다(client):
    async def go():
        await client.ocr_load(cuda_graph=True)
        pid = client._process.pid
        await client.ocr_unload()
        assert not client.ocr_loaded and client._process is not None and client._process.pid == pid
        got = await client.ocr_read(_b64("다시"), cuda_graph=True)  # 내린 뒤 그림이 오면 worker 가 다시 올린다
        assert got["text"] == "읽음:다시" and client.ocr_loaded
        await client.stop()
        await client.ocr_unload()  # 내려가 있으면 띄우지 않는다
        assert client._process is None
    _run(go)


def test_OCR_없이_뜬_worker_면_사이드카로_넘기게_Unavailable(client, monkeypatch):
    monkeypatch.setenv("FAKE_OCR", "0")

    async def go():
        with pytest.raises(combined_gpu.CombinedGpuUnavailable) as err:
            await client.ocr_load(cuda_graph=True)
        assert not isinstance(err.value, combined_gpu.CombinedGpuOcrDied)
        await client.stop()
    _run(go)


@pytest.fixture
def ocr_pc(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_OCR_ENABLED", True)
    monkeypatch.setattr(config, "IMAGE_OCR_IN_WORKER", True)
    monkeypatch.setattr(config, "COMBINED_GPU_WORKER_ENABLED", True)
    monkeypatch.setattr(config, "IMAGE_OCR_PYTHON_EXECUTABLE", sys.executable)
    monkeypatch.setattr(config, "COMBINED_GPU_PYTHON_EXECUTABLE", sys.executable)
    monkeypatch.setattr(model_status, "image_ocr_accelerator", lambda: "cuda")


def test_Windows_CUDA_면_worker_에_OCR_인자를_넘긴다(ocr_pc):
    args = combined_gpu.CombinedGpuClient()._args()
    assert args[args.index("--ocr-runtime") + 1].endswith("varco_ocr_inference.py") and "--ocr-model-dir" in args


@pytest.mark.parametrize("change", ["mps", "xpu", "off", "env_off", "worker_off", "other_python"])
def test_worker_안_OCR_을_쓰지_않는_경우(ocr_pc, monkeypatch, change):
    if change in ("mps", "xpu"):
        monkeypatch.setattr(model_status, "image_ocr_accelerator", lambda: change)
    elif change == "off":
        monkeypatch.setattr(config, "IMAGE_OCR_ENABLED", False)
    elif change == "worker_off":  # 통합 worker 를 env 로 껐다 — 그림마다 worker 를 불러 보지 않고 바로 사이드카
        monkeypatch.setattr(config, "COMBINED_GPU_WORKER_ENABLED", False)
    elif change == "env_off":  # SECUREDOC_IMAGE_OCR_IN_WORKER=0 — 5차 측정에서 견주어 볼 수 있게
        monkeypatch.setattr(config, "IMAGE_OCR_IN_WORKER", False)
    else:
        monkeypatch.setattr(config, "IMAGE_OCR_PYTHON_EXECUTABLE", r"C:\other\python.exe")
    assert not combined_gpu.ocr_in_worker()
    assert "--ocr-runtime" not in combined_gpu.CombinedGpuClient()._args()


# ── 엔진(image_ocr) — 가짜 worker 객체 ─────────────────────────────────────────────────

class FakeWorker:
    """ocr_read 결과를 차례로 내는 가짜 통합 worker. 'die'·'graph'·'cuda'·'oom' 은 그 오류를 낸다."""

    def __init__(self, *outcomes, ready=True):
        self.outcomes, self.ready_ok = list(outcomes), ready
        self.ocr_loaded = False
        self.calls: list[str] = []
        self.graphs: list[bool] = []

    async def ensure_ready(self):
        if not self.ready_ok:
            raise combined_gpu.CombinedGpuUnavailable("disabled")

    async def ocr_load(self, *, cuda_graph):
        self.calls.append("load")
        self.graphs.append(cuda_graph)
        self.ocr_loaded = True
        return {"device": "cuda", "batch": 4, "budget": 4608, "graph": cuda_graph, "card_free_mb": 3162}

    async def ocr_read(self, _image, *, cuda_graph):
        self.calls.append("read")
        what = self.outcomes.pop(0)
        if what in ("die", "graph", "cuda"):
            self.ocr_loaded = False  # worker 가 끝났다
            if what == "die":
                raise combined_gpu.CombinedGpuOcrDied("combined GPU worker exited")
            raise combined_gpu.CombinedGpuOcrError(f"{what}_failed: illegal memory access")
        if what == "oom":
            raise combined_gpu.CombinedGpuOcrError("OutOfMemoryError")
        return {"text": what, "tiles": [], "secs": 0.1}

    async def ocr_unload(self):
        self.calls.append("unload")
        self.ocr_loaded = False


@pytest.fixture
def proc(monkeypatch):
    p = image_ocr.VarcoOcrProcess()
    monkeypatch.setattr(image_ocr, "_process", p)
    monkeypatch.setattr(image_ocr.model_status, "image_ocr_ready", lambda: True)
    monkeypatch.setattr(combined_gpu, "ocr_in_worker", lambda: True)
    for name in ("free_commit_mb", "free_physical_mb", "commit_limit_mb"):
        monkeypatch.setattr(image_ocr, name, lambda: None)
    sidecar = []

    async def sidecar_read(_payload):
        sidecar.append(1)
        return {"results": [{"text": "사이드카", "tiles": [], "secs": 0.1}]}

    async def nothing():
        return None

    monkeypatch.setattr(p, "_ensure_process", nothing)
    monkeypatch.setattr(p, "_rpc", sidecar_read)
    monkeypatch.setattr(image_ocr, "gpu_worker_first", nothing)
    p.sidecar = sidecar
    return p


def _use(monkeypatch, worker):
    monkeypatch.setattr(combined_gpu, "get_worker", lambda: worker)
    return worker


def _read(p, n=1):
    async def go():
        return [await p.read(b"png") for _ in range(n)]
    return asyncio.run(go())


def test_worker_안에서_한_번_올리고_그림마다_읽으며_준비를_로그에_남긴다(proc, monkeypatch, caplog):
    w = _use(monkeypatch, FakeWorker("가", "나"))
    with caplog.at_level("INFO", logger="securedoc.engine.image_ocr"):
        got = _read(proc, 2)
    assert [g["text"] for g in got] == ["가", "나"] and w.calls == ["load", "read", "read"] and not proc.sidecar
    assert image_ocr.status()["inWorker"] is True
    assert "통합 worker 안에서 준비" in caplog.text and "'batch': 4" in caplog.text


def test_graph_failed_면_그래프를_끄고_worker_를_다시_띄워_같은_그림을_다시_읽는다(proc, monkeypatch):
    w = _use(monkeypatch, FakeWorker("graph", "가"))
    assert _read(proc)[0]["text"] == "가"
    assert proc.cuda_graph is False and w.graphs == [True, False] and w.calls == ["load", "read", "load", "read"]


def test_답_없이_한_번이면_그래프를_켠_채_두_번이면_끄고_다시_읽는다(proc, monkeypatch):
    w = _use(monkeypatch, FakeWorker("die", "가"))
    assert _read(proc)[0]["text"] == "가" and proc.cuda_graph is True and w.graphs == [True, True]
    w = _use(monkeypatch, FakeWorker("die", "die", "나"))
    assert _read(proc)[0]["text"] == "나" and proc.cuda_graph is False and w.graphs == [True, True, False]


def test_그래프를_끄고도_worker_를_끝내면_이번_실행_동안_사이드카로(proc, monkeypatch):
    proc.cuda_graph = False
    w = _use(monkeypatch, FakeWorker("cuda"))
    assert _read(proc)[0]["text"] == "사이드카", "같은 그림은 사이드카로 읽는다"
    assert proc._worker_ocr_off and proc.sidecar == [1] and image_ocr.status()["inWorker"] is False
    assert _read(proc)[0]["text"] == "사이드카" and w.calls == ["load", "read"], "그 뒤로 worker 에 OCR 을 보내지 않는다"


def test_worker_를_못_쓰면_사이드카로_읽는다(proc, monkeypatch):
    _use(monkeypatch, FakeWorker(ready=False))
    assert _read(proc)[0]["text"] == "사이드카" and proc.sidecar == [1]


def test_올리기_실패와_읽기_실패는_그_그림만_미검사(proc, monkeypatch):
    w = _use(monkeypatch, FakeWorker())

    async def bad_load(*, cuda_graph):
        raise combined_gpu.CombinedGpuOcrError("OutOfMemoryError: 가중치")

    monkeypatch.setattr(w, "ocr_load", bad_load)
    assert _read(proc) == [None] and proc.last_failure == image_ocr.LAUNCH_FAILED_REASON
    w = _use(monkeypatch, FakeWorker("oom"))
    assert _read(proc) == [None] and proc.last_failure == image_ocr.READ_FAILED_REASON and not proc.sidecar


def test_worker_안에서는_올리기_커밋_기준이_가중치_몫뿐이다(proc, monkeypatch):
    # 사이드카 기준(9,216MB)에는 모자라도 worker 기준(바닥 몫 약 2GB 를 뺀 값)이면 올린다
    w = _use(monkeypatch, FakeWorker("가"))
    monkeypatch.setattr(image_ocr, "free_commit_mb", lambda: config.IMAGE_OCR_WORKER_LAUNCH_FREE_COMMIT_MB + 100)
    assert config.IMAGE_OCR_WORKER_LAUNCH_FREE_COMMIT_MB + 100 < config.IMAGE_OCR_LAUNCH_FREE_COMMIT_MB
    assert _read(proc)[0]["text"] == "가"
    w = _use(monkeypatch, FakeWorker("나"))
    monkeypatch.setattr(image_ocr, "free_commit_mb", lambda: config.IMAGE_OCR_WORKER_LAUNCH_FREE_COMMIT_MB - 1)
    assert _read(proc) == [None] and proc.last_failure == image_ocr.LOW_MEMORY_REASON and w.calls == []
    assert proc.memory_short["kind"] == "commit"
    assert proc.memory_short["needMb"] == config.IMAGE_OCR_WORKER_LAUNCH_FREE_COMMIT_MB


def test_올라가_있을_때_물리가_바닥나면_가중치만_내린다(proc, monkeypatch):
    w = _use(monkeypatch, FakeWorker("가"))
    w.ocr_loaded = True
    monkeypatch.setattr(image_ocr, "free_physical_mb", lambda: config.IMAGE_OCR_READ_FREE_PHYS_MB - 1)
    assert _read(proc) == [None] and proc.last_failure == image_ocr.LOW_MEMORY_REASON
    assert w.calls == ["unload"], "PII·인젝션 프로세스는 남기고 OCR 가중치만 내린다"


def test_유휴가_지나면_worker_는_두고_가중치만_내린다(proc, monkeypatch):
    w = _use(monkeypatch, FakeWorker("가"))
    monkeypatch.setattr(config, "IMAGE_OCR_IDLE_UNLOAD_SEC", 0.01)

    async def go():
        await proc.read(b"png")
        await asyncio.sleep(0.1)

    asyncio.run(go())
    assert w.calls == ["load", "read", "unload"] and not w.ocr_loaded
