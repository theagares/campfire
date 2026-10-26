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


def test_pii_keep_windows_are_deduplicated_in_document_coordinates():
    detector = object.__new__(EncoderPiiDetector)
    text = "가나다라마바사아자차"
    windows = [(text[:8], 0), (text[4:], 4)]
    kept = [
        [{"label": "OG_WORKPLACE", "begin": 5, "end": 7, "score": 0.01}],
        [{"label": "OG_WORKPLACE", "begin": 1, "end": 3, "score": 0.01}],
    ]

    assert detector.decode_keep_windows(text, windows, kept) == [{
        "type": "ORGANIZATION",
        "start": 5,
        "end": 7,
        "text": text[5:7],
        "maskProbability": 0.01,
        "decision": "KEEP",
    }]


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


def test_combined_keep_diagnostics_follow_the_same_slices(monkeypatch):
    client = _client(monkeypatch)
    payloads: list[dict[str, Any]] = []

    async def request(payload):
        payloads.append(payload)
        texts = payload.get("pii_texts", payload.get("texts", []))
        records = payload.get("injection_records", payload.get("records", []))
        return {
            "pii_entities": [[] for _ in texts],
            "pii_kept": [[{"form": value}] for value in texts],
            "injection_results": [{"r": row["tool_response"]} for row in records],
        }

    client._request = request
    pii, kept, injection = asyncio.run(client.infer_parallel_with_pii_keep(
        ["p0", "p1"], [{"tool_response": "i0"}],
    ))

    assert pii == [[], []]
    assert [row[0]["form"] for row in kept] == ["p0", "p1"]
    assert injection == [{"r": "i0"}]
    assert all(payload["include_pii_keep"] is True for payload in payloads)


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


# ── 인젝션 sidecar: 느린 CPU 에서 예열·요청이 시간 상한을 넘던 문제 (v0.2.32 릴리스 E2E) ──


def _injection_detector(monkeypatch, device):
    from app.core.detectors.injection.llm_mcp import InjectionLlmMcpDetector

    detector = InjectionLlmMcpDetector()
    detector.residency.device = device

    async def no_watch():
        return None

    monkeypatch.setattr(detector, "_idle_watcher", no_watch)
    return detector


def test_injection_respawns_when_warmup_kills_process(monkeypatch):
    # 예열이 120초를 넘기면 _infer_batch 가 프로세스를 내리고 예외는 삼켜진다. 그대로 로드 완료로
    # 두면 첫 실요청이 없는 프로세스로 가 AssertionError → /jobs 500 (Intel mac·Windows 릴리스 E2E).
    detector = _injection_detector(monkeypatch, "cpu")
    spawned = []

    async def spawn():
        spawned.append(1)
        detector._process = _FakeProc()

    async def slow_warmup(texts, *, user_prompts=None):
        detector._process = None  # 시간 상한에 걸려 내려진 상태
        raise RuntimeError("timed out")

    monkeypatch.setattr(detector, "_spawn_process", spawn)
    monkeypatch.setattr(detector, "_infer_batch", slow_warmup)
    asyncio.run(detector._ensure_process())
    assert len(spawned) == 2 and detector._process is not None


@pytest.mark.parametrize("device, expected", [("cpu", [1]), ("cuda", [8])])
def test_injection_warmup_batch_only_on_cuda(monkeypatch, device, expected):
    detector = _injection_detector(monkeypatch, device)
    sizes = []

    async def infer(texts, *, user_prompts=None):
        sizes.append(len(texts))
        return [{} for _ in texts]

    monkeypatch.setattr(config, "INJECTION_BATCH_SIZE", 8)
    monkeypatch.setattr(detector, "_infer_batch", infer)
    asyncio.run(detector._warmup())
    assert sizes == expected


@pytest.mark.parametrize("device, max_size", [("cpu", 2), ("cuda", 8)])
def test_injection_requests_are_small_on_cpu(monkeypatch, device, max_size):
    detector = _injection_detector(monkeypatch, device)
    sizes = []

    async def ready():
        pass

    async def infer_batch(texts, *, user_prompts=None):
        sizes.append(len(texts))
        return [{"is_injection": False, "scores": {}} for _ in texts]

    async def infer(text, user_prompt=None):
        sizes.append(1)
        return {"is_injection": False, "scores": {}}

    monkeypatch.setattr(config, "INJECTION_BATCH_SIZE", 8)
    monkeypatch.setattr(config, "INJECTION_CPU_BATCH_SIZE", 2)
    monkeypatch.setattr(detector, "_ensure_process", ready)
    monkeypatch.setattr(detector, "_infer_batch", infer_batch)
    monkeypatch.setattr(detector, "_infer", infer)
    results = asyncio.run(detector.classify_batch_local([f"문장 {i}" for i in range(8)]))
    assert len(results) == 8 and max(sizes) == max_size and sum(sizes) == 8


def test_sidecar_path_loads_pii_before_injection(monkeypatch):
    # 처음 검사에서 PII·EXAONE 을 동시에 올리면 느린 CPU 에서 PII 로딩이 60초 상한을 넘겼다
    # (Intel mac 설치본 E2E). PII 를 먼저 올린 뒤 추론은 병렬로 한다.
    from app.core.detectors import registry

    order: list[str] = []

    class Pii:
        async def _ensure_process(self):
            order.append("pii_load")

        async def detect(self, text, *, meta=None):
            return []

    class Inj:
        async def classify_batch_local(self, texts, *, user_prompts=None):
            order.append("injection_classify")
            return [{"is_injection": False, "scores": {}} for _ in texts]

        async def finalize_batch_local(self, texts, results, *, metas=None):
            return [[] for _ in texts]

    monkeypatch.setattr(model_status, "all_ready", lambda: True)
    monkeypatch.setattr(config, "COMBINED_GPU_WORKER_ENABLED", False)
    monkeypatch.setattr(registry, "get_pii_detector", lambda: Pii())
    monkeypatch.setattr(registry, "get_injection_detector", lambda: Inj())
    monkeypatch.setattr(
        orchestrator.injection_chunker, "split_injection_chunks",
        lambda text: [{"text": text, "offset": 0}],
    )

    asyncio.run(orchestrator.run_pipeline(text="평범한 문서입니다."))
    assert order.index("pii_load") < order.index("injection_classify")


def test_pii_respawns_when_warmup_kills_process(monkeypatch):
    # 예열 중 sidecar 가 죽으면 _rpc 가 버리고(_process=None) 예외는 삼켜진다. 그대로 준비 완료로 두면
    # 첫 실요청이 죽은 프로세스에 써서 "Connection lost" 로 /jobs 500 (v0.2.49 Windows 릴리스 E2E).
    detector = object.__new__(EncoderPiiDetector)
    detector._process, detector._process_ready, detector._proc_lock = None, False, asyncio.Lock()
    spawned = []

    async def spawn():
        spawned.append(1)
        detector._process = _FakeProc()

    async def dying_warmup(texts):
        detector._process = None
        raise RuntimeError("서브프로세스가 응답 없이 종료됨")

    monkeypatch.setattr(detector, "_spawn_process", spawn)
    monkeypatch.setattr(detector, "_infer_batch", dying_warmup)
    asyncio.run(detector._ensure_process())
    assert len(spawned) == 2 and detector._process is not None and detector._process_ready


def test_pii_write_to_dead_sidecar_reports_stderr_and_discards():
    class _Dead(_FakeProc):
        def __init__(self):
            super().__init__()
            self.stdout = self.stderr = self

        async def drain(self):
            raise ConnectionResetError("Connection lost")

        async def read(self):
            return b"Traceback (most recent call last): ... MemoryError"

    detector = object.__new__(EncoderPiiDetector)
    detector._next_id, detector._request_lock, detector._process = 0, asyncio.Lock(), _Dead()
    with pytest.raises(RuntimeError, match="이미 종료됨.*MemoryError"):
        asyncio.run(detector._rpc({"texts": ["x"]}))
    assert detector._process is None  # 다음 요청이 새로 띄운다
