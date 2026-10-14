"""병렬 탐지(#189) 리뷰에서 나온 실패 경로를 지킨다.

- 요청 하나의 크기·시간이 문서 길이에 비례해 커지지 않는다(긴 문서에서 64KiB 응답 줄을 넘어 실패했다)
- 통합 GPU worker 는 한 번의 실패·취소·설치 직후 상태로 앱 실행 내내 꺼지지 않는다
- 빈 청크는 워커에 보내지 않는다
- 문서 하나의 Solar 동시 호출은 DETECT_CONCURRENCY 를 넘지 않는다
- 유휴 시간이 지나면 통합 worker 를 내린다
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import config
from app.core import model_status
from app.core.detectors import combined_gpu
from app.core.detectors.pii.encoder import EncoderPiiDetector
from app.core.pipeline import orchestrator


def test_pii_sidecar_requests_are_bounded():
    detector = object.__new__(EncoderPiiDetector)
    sizes: list[int] = []

    async def ready():
        pass

    async def infer(texts):
        sizes.append(len(texts))
        return [[{"label": "PS_NAME", "begin": 0, "end": 2}] for _ in texts]

    class _Residency:
        def touch(self):
            pass

    detector._ensure_process = ready
    detector._infer_batch = infer
    detector.residency = _Residency()
    text = "가나다라마바사 " * 3000  # 24k자 — 예전엔 요청 하나로 갔다
    windows = detector.prepare_windows(text)

    items = asyncio.run(detector.detect(text))

    assert len(windows) > config.PII_WINDOWS_PER_RPC
    assert sizes and max(sizes) <= config.PII_WINDOWS_PER_RPC
    assert sum(sizes) == len(windows)
    assert {(it["start"], it["end"]) for it in items} == {(off, off + 2) for _, off in windows}


def _client(monkeypatch) -> combined_gpu.CombinedGpuClient:
    monkeypatch.setattr(config, "COMBINED_GPU_WORKER_ENABLED", True)
    return combined_gpu.CombinedGpuClient()


def test_combined_requests_are_sliced(monkeypatch):
    client = _client(monkeypatch)
    payloads: list[dict[str, Any]] = []

    async def request(payload):
        payloads.append(payload)
        out: dict[str, Any] = {}
        if "pii_texts" in payload or "texts" in payload:
            out["pii_entities"] = [[{"w": t}] for t in payload.get("pii_texts", payload.get("texts", []))]
        if "injection_records" in payload or "records" in payload:
            records = payload.get("injection_records", payload.get("records", []))
            out["injection_results"] = [{"r": r["tool_response"]} for r in records]
        return out

    client._request = request
    pii = [f"p{i}" for i in range(150)]
    records = [{"tool_response": f"i{i}"} for i in range(20)]

    pii_out, inj_out = asyncio.run(client.infer_parallel(pii, records))

    assert [row[0]["w"] for row in pii_out] == pii            # 순서 유지
    assert [row["r"] for row in inj_out] == [r["tool_response"] for r in records]
    assert all(len(p.get("pii_texts", p.get("texts", []))) <= config.COMBINED_GPU_PII_WINDOWS_PER_REQUEST
               for p in payloads)
    assert [p["command"] for p in payloads] == ["parallel", "pii", "pii"]  # 한쪽이 빈 회차는 단일 명령


class _FakeProc:
    def __init__(self):
        self.returncode = None
        self.stdin = self

    def write(self, _data):
        pass

    async def drain(self):
        pass

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _with_process(client, monkeypatch, read):
    async def ready():
        if client._process is None:
            client._process = _FakeProc()
            client._ready = True

    monkeypatch.setattr(client, "ensure_ready", ready)
    monkeypatch.setattr(client, "_read_response", read)


def test_cancelled_request_does_not_disable_worker(monkeypatch):
    client = _client(monkeypatch)

    async def read(_timeout, *, expected_id):
        raise asyncio.CancelledError

    _with_process(client, monkeypatch, read)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(client._request({"command": "pii", "texts": ["x"]}))
    assert client._process is None and not client._disabled


def test_worker_errors_disable_only_after_repeated_failures(monkeypatch):
    client = _client(monkeypatch)

    async def read(_timeout, *, expected_id):
        return {"id": expected_id, "error": "boom"}

    _with_process(client, monkeypatch, read)

    async def run():
        for attempt in range(config.COMBINED_GPU_MAX_FAILURES):
            assert not client._disabled, f"{attempt}번 실패로 꺼졌다"
            with pytest.raises(combined_gpu.CombinedGpuUnavailable):
                await client._request({"command": "pii", "texts": ["x"]})
        assert client._disabled

    asyncio.run(run())


def test_spawn_policy_no_cuda_vs_downloading(monkeypatch):
    client = _client(monkeypatch)

    async def no_cuda():
        raise combined_gpu.CombinedGpuNoCuda("CUDA is not available")

    monkeypatch.setattr(client, "_spawn", no_cuda)
    with pytest.raises(combined_gpu.CombinedGpuUnavailable):
        asyncio.run(client.ensure_ready())
    assert client._disabled  # CUDA 는 나중에 생기지 않는다

    client = _client(monkeypatch)

    async def weights_missing():
        raise combined_gpu.CombinedGpuUnavailable("model files missing")

    monkeypatch.setattr(client, "_spawn", weights_missing)
    monkeypatch.setattr(model_status, "all_ready", lambda: False)

    async def run():
        for _ in range(config.COMBINED_GPU_MAX_FAILURES + 1):
            with pytest.raises(combined_gpu.CombinedGpuUnavailable):
                await client.ensure_ready()

    asyncio.run(run())
    assert not client._disabled and client._failures == 0  # 받는 중에는 세지 않는다


def test_idle_worker_is_unloaded(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(config, "INJECTION_LLM_IDLE_TIMEOUT_SEC", 0.05)

    async def run():
        client._process = _FakeProc()
        client._ready = True
        client._last_used = asyncio.get_running_loop().time()
        await asyncio.wait_for(client._idle_watcher(), timeout=2.0)

    asyncio.run(run())
    assert client._process is None and not client._disabled


def test_empty_injection_chunks_are_not_sent_to_worker(monkeypatch):
    sent: list[list[dict]] = []

    class Worker:
        async def infer_parallel(self, pii_texts, records):
            sent.append(records)
            return [[] for _ in pii_texts], [{"is_injection": True, "scores": {}} for _ in records]

    class Pii:
        def prepare_windows(self, text):
            return [(text, 0)] if text.strip() else []

        def decode_windows(self, text, windows, entities):
            return []

    class Inj:
        async def classify_batch_local(self, texts, *, user_prompts=None):
            return []

        async def finalize_batch_local(self, texts, results, *, metas=None):
            return []

    monkeypatch.setattr(config, "COMBINED_GPU_WORKER_ENABLED", True)
    monkeypatch.setattr(combined_gpu, "get_worker", lambda: Worker())
    chunks = [{"text": "", "offset": 0}, {"text": "   ", "offset": 0}, {"text": "본문", "offset": 3}]

    _, _, classified = asyncio.run(orchestrator._try_combined_local_inference(
        Pii(), Inj(), "   본문", chunks, user_prompt="요약해줘",
    ))

    assert [r["tool_response"] for r in sent[0]] == ["본문"]
    assert [result["is_injection"] for _, result in classified] == [False, False, True]


def test_solar_concurrency_is_capped_per_document(monkeypatch):
    active = peak = 0

    class Inj:
        async def finalize_batch_local(self, texts, results, *, metas=None):
            nonlocal active, peak
            active += len(texts)  # 마무리 한 번에 실린 청크 수만큼 Solar 가 동시에 나간다
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= len(texts)
            return [[] for _ in texts]

    classified = [({"text": f"t{i}", "offset": i}, {"is_injection": True}) for i in range(64)]
    asyncio.run(orchestrator._finalize_staged_injection(Inj(), "x" * 100, classified))
    assert peak <= config.DETECT_CONCURRENCY
