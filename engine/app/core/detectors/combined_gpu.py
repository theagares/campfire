"""Client for the single-process, two-stream CUDA inference worker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from typing import Any

from app import config
from app.core import model_status
from app.core.detectors.injection import backbone

logger = logging.getLogger("securedoc.engine.combined_gpu")


def cuda_torch_path() -> str | None:
    """Windows 에서 모델 받기가 따로 깔아 둔 CUDA torch(이미지 검사가 쓰는 gpu_torch) 폴더 — 있으면 통합 worker 도 앞세운다.

    엔진 venv 의 torch 는 CPU 판이라 그대로 띄우면 worker 가 "CUDA 없음" 으로 끝나 이번 실행 동안 꺼지고, PII·인젝션이
    GPU 가 있는 PC 에서도 CPU 사이드카로 돌았다(v0.2.62 Windows RTX 5070: nvidia-smi 에 OCR 만 있었다). 이미지 검사
    사이드카는 같은 폴더를 앞세워 CUDA 로 돈다(image_ocr._spawn_env). 따로 지정한 파이썬이면 그 파이썬을 그대로 쓴다.
    """
    if config.COMBINED_GPU_PYTHON_EXECUTABLE != sys.executable or config.IMAGE_OCR_PYTHON_OVERRIDE:
        return None
    if model_status.image_ocr_accelerator() != "cuda" or not model_status.image_ocr_runtime_ready():
        return None
    return str(config.IMAGE_OCR_GPU_TORCH_DIR)


def ocr_in_worker() -> bool:
    """이미지 OCR(VARCO)을 통합 worker 안에서 읽는가 — CUDA·torch 바닥(Windows 커밋 약 2GB·VRAM 약 0.4GB)을 PII·인젝션과
    한 번만 쓴다. 같은 파이썬(같은 torch·transformers)으로 뜨는 CUDA worker 일 때만. 맥(MPS)·Intel(XPU)은 지금처럼 사이드카다.
    실제 CUDA 여부는 worker 가 뜰 때 확인한다(못 쓰면 OCR 도 사이드카로)."""
    return (config.IMAGE_OCR_ENABLED and config.IMAGE_OCR_IN_WORKER
            and config.IMAGE_OCR_PYTHON_EXECUTABLE == config.COMBINED_GPU_PYTHON_EXECUTABLE
            and model_status.image_ocr_accelerator() in ("cuda", "external"))


class CombinedGpuUnavailable(RuntimeError):
    """The combined CUDA path cannot be used; callers should use sidecars."""


class CombinedGpuNoCuda(CombinedGpuUnavailable):
    """이 기기에 CUDA 가 없다 — 앱을 다시 켜도 바뀌지 않으므로 이번 실행 동안 끈다."""


class CombinedGpuOcrDied(CombinedGpuUnavailable):
    """OCR 요청 중에 worker 가 답 없이 끝났다(멎음·외부 종료·메모리 부족) — image_ocr 가 다시 띄워 다시 읽을지 정한다."""


class CombinedGpuOcrError(RuntimeError):
    """worker 가 OCR 요청에 오류로 답했다. graph_failed·cuda_failed 면 worker 는 이미 끝나 정리했다."""


class CombinedGpuClient:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._ready = False
        self._disabled = not config.COMBINED_GPU_WORKER_ENABLED
        self._failures = 0
        self._last_used = 0.0
        self._process_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._next_id = 0
        self._prewarm_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self.ocr_available = False  # 이 worker 가 OCR 인자를 받고 떴다(ready 의 ocr)
        self.ocr_loaded = False  # OCR 가중치가 올라가 있다 — 프로세스가 내려가면 같이 사라진다

    def _runtime_script(self):
        return config.APP_DIR / "models" / "combined_gpu_inference.py"

    def _args(self) -> list[str]:
        return [
            config.COMBINED_GPU_PYTHON_EXECUTABLE,
            str(self._runtime_script()),
            "--pii-engine-dir", str(config.PII_ENGINE_DIR),
            "--pii-model-seed", config.PII_MODEL_SEED,
            "--pii-dtype", config.PII_DTYPE,
            "--pii-batch-size", str(config.PII_BATCH_SIZE),
            "--pii-max-length", str(config.PII_MAX_LENGTH),
            "--injection-engine-dir", str(config.INJECTION_ENGINE_DIR),
            "--injection-variant", config.INJECTION_VARIANT,
            "--injection-dtype", config.INJECTION_DTYPE,
            "--injection-max-seq-len", str(config.INJECTION_MAX_SEQ_LEN),
            "--injection-batch-size", str(config.INJECTION_BATCH_SIZE),
            "--injection-pad-tokens", str(config.INJECTION_PAD_TOKENS),
            "--injection-compile" if config.INJECTION_COMPILE else "--no-injection-compile",
            (
                "--injection-compact-attentions"
                if config.INJECTION_COMPACT_ATTENTIONS
                else "--no-injection-compact-attentions"
            ),
            (
                "--injection-compact-hidden-states"
                if config.INJECTION_COMPACT_HIDDEN_STATES
                else "--no-injection-compact-hidden-states"
            ),
            *(
                [
                    "--ocr-runtime", str(config.IMAGE_OCR_ENGINE_DIR / "runtime" / "varco_ocr_inference.py"),
                    "--ocr-model-dir", str(config.IMAGE_OCR_ENGINE_DIR / "models"),
                    "--ocr-batch", str(config.IMAGE_OCR_BATCH),
                    "--ocr-token-budget", str(config.IMAGE_OCR_TOKEN_BUDGET),
                ]
                if ocr_in_worker()
                else []
            ),
        ]

    async def _spawn(self) -> None:
        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "TOKENIZERS_PARALLELISM": "false",
        }
        if backbone.is_cached():
            env["HF_HUB_OFFLINE"] = "1"
            env["TRANSFORMERS_OFFLINE"] = "1"
        if gpu_torch := cuda_torch_path():
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [gpu_torch, os.environ.get("PYTHONPATH")]))
            logger.info("[combined_gpu] CUDA torch(gpu_torch)를 앞세워 통합 worker 를 띄운다")
        self._process = await asyncio.create_subprocess_exec(
            *self._args(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=config.SUBPROCESS_STREAM_LIMIT,
        )
        payload = await self._read_response(
            config.COMBINED_GPU_LOAD_TIMEOUT_SEC, expected_id=None
        )
        # 워커는 가중치를 올리기 전에 CUDA 부터 확인한다 — 없으면 바로 이 답이 온다.
        if "CUDA is not available" in str(payload.get("error", "")) or (
            payload.get("ready") and payload.get("device") != "cuda"
        ):
            raise CombinedGpuNoCuda(f"combined GPU worker unavailable: {payload!s}")
        if not payload.get("ready"):
            raise CombinedGpuUnavailable(
                f"combined GPU worker unavailable: {payload.get('error', payload)!s}"
            )
        self.ocr_available = bool(payload.get("ocr"))
        self.ocr_loaded = False
        self._ready = True
        self._last_used = asyncio.get_running_loop().time()
        self._idle_task = asyncio.create_task(self._idle_watcher())

    def _note_failure(self) -> None:
        """연속 실패가 쌓이면 이번 실행 동안 끈다. 한 번의 실패로 끄지 않는다 — 문서 하나의
        이상 입력이나 일시 오류가 이후 모든 요청을 느린 개별 sidecar 로 보내지 않게."""
        self._failures += 1
        if self._failures >= config.COMBINED_GPU_MAX_FAILURES:
            self._disabled = True

    async def ensure_ready(self) -> None:
        if self._disabled:
            raise CombinedGpuUnavailable("combined GPU worker is disabled")
        alive = self._process is not None and self._process.returncode is None
        if self._ready and alive:
            return
        async with self._process_lock:
            alive = self._process is not None and self._process.returncode is None
            if self._ready and alive:
                return
            await self.stop()
            try:
                await self._spawn()
            except asyncio.CancelledError:
                self._drop_process()
                raise
            except CombinedGpuNoCuda as exc:
                # 예전엔 이 길을 조용히 탔다 — PII·인젝션이 CPU 로 도는 까닭을 로그에서 못 찾았다.
                logger.warning("[combined_gpu] CUDA 를 못 써 이번 실행 동안 끈다 — PII·인젝션은 개별(CPU) 사이드카로: %s", exc)
                await self.stop()
                self._disabled = True
                raise
            except BaseException as exc:
                await self.stop()
                # 설치 직후 가중치를 받는 중이면 실패가 당연하다 — 받은 뒤 다시 시도할 수 있게 세지 않는다.
                if model_status.all_ready():
                    self._note_failure()
                if isinstance(exc, CombinedGpuUnavailable):
                    raise
                raise CombinedGpuUnavailable(str(exc)) from exc

    async def _read_response(
        self, timeout: float, *, expected_id: str | None
    ) -> dict[str, Any]:
        proc = self._process
        assert proc is not None and proc.stdout is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CombinedGpuUnavailable("combined GPU worker response timed out")
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not line:
                stderr = b""
                if proc.stderr is not None:
                    stderr = await proc.stderr.read()
                raise CombinedGpuUnavailable(
                    "combined GPU worker exited: "
                    + stderr.decode(errors="replace")[-2000:]
                )
            try:
                payload = json.loads(line.decode())
            except json.JSONDecodeError:
                continue
            if expected_id is None or payload.get("id") == expected_id:
                return payload

    async def _request(
        self, payload: dict[str, Any], *, timeout: float | None = None, ocr: bool = False, spawn: bool = True
    ) -> dict[str, Any]:
        """요청 하나를 보내고 답 하나를 받는다. ocr=True(이미지 OCR 요청): 오류 답은 돌려주고(worker 를 끄지 않는다 — 그림 한 장의
        실패로 PII·인젝션을 개별 sidecar 로 보내지 않게), 답 없이 끝나면 CombinedGpuOcrDied 로 알리되 실패로 세지 않는다.
        spawn=False: 떠 있지 않으면 띄우지 않고 CombinedGpuUnavailable(가중치 내리기용)."""
        request_id = str(self._next_id)
        self._next_id += 1
        request = {"id": request_id, **payload}
        for attempt in range(2):
            if spawn:
                await self.ensure_ready()
            async with self._request_lock:
                proc = self._process
                if proc is None or proc.returncode is not None or not self._ready:
                    # 줄 서 있는 사이 내려갔다(유휴 해제·OCR 오류로 끝남) — 한 번 새로 띄워 받는다. 예전엔 바로 개별 sidecar 로
                    # 갔는데, 이제 그림 하나가 worker 를 다시 띄우게 하면 그동안 기다리던 PII·인젝션 요청이 모두 밀려났다.
                    if spawn and attempt == 0:
                        continue
                    raise CombinedGpuUnavailable("combined GPU worker was unloaded")
                assert proc.stdin is not None
                self._last_used = asyncio.get_running_loop().time()
                try:
                    proc.stdin.write(
                        (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
                    )
                    await proc.stdin.drain()
                    response = await self._read_response(
                        timeout or config.DETECT_INFER_TIMEOUT_SEC, expected_id=request_id
                    )
                except asyncio.CancelledError:
                    # 응답을 읽다 만 프로세스는 다음 요청과 줄이 어긋난다 — 버리되, 끄지는 않는다.
                    self._drop_process()
                    raise
                except BaseException as exc:
                    await self.stop()
                    if ocr:  # 그림이 worker 를 끝냈다 — 세지 않는다(쌓이면 PII·인젝션까지 이번 실행 동안 개별로 밀린다)
                        raise CombinedGpuOcrDied(str(exc)) from exc
                    self._note_failure()
                    if isinstance(exc, CombinedGpuUnavailable):
                        raise
                    raise CombinedGpuUnavailable(str(exc)) from exc
                self._last_used = asyncio.get_running_loop().time()
                if ocr and str(response.get("error", "")).startswith(("graph_failed", "cuda_failed")):
                    # 답하고 끝나는 중이다 — 다음 탐지 요청에 죽어 가는 프로세스를 넘기지 않게 지금 정리한다
                    await self.stop()
            break
        if ocr:
            return response
        if "error" in response:
            await self.fail()
            raise CombinedGpuUnavailable(
                f"combined GPU inference failed: {response['error']}"
            )
        self._failures = 0
        return response

    async def infer_parallel(
        self,
        pii_texts: list[str],
        injection_records: list[dict[str, str]],
    ) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
        pii_entities, _, injection_results = await self._infer_parallel(
            pii_texts, injection_records, include_pii_keep=False
        )
        return pii_entities, injection_results

    async def infer_parallel_with_pii_keep(
        self,
        pii_texts: list[str],
        injection_records: list[dict[str, str]],
    ) -> tuple[
        list[list[dict[str, Any]]],
        list[list[dict[str, Any]]],
        list[dict[str, Any]],
    ]:
        """시연 대시보드용 MLP KEEP 판정까지 함께 받는다."""
        return await self._infer_parallel(
            pii_texts, injection_records, include_pii_keep=True
        )

    async def _infer_parallel(
        self,
        pii_texts: list[str],
        injection_records: list[dict[str, str]],
        *,
        include_pii_keep: bool,
    ) -> tuple[
        list[list[dict[str, Any]]],
        list[list[dict[str, Any]]],
        list[dict[str, Any]],
    ]:
        """문서를 여러 요청으로 나눠 보낸다. 문서 전체를 한 요청으로 보내면 응답 한 줄이
        문서 길이에 비례해 커지고 시간 상한도 문서 전체에 걸린다(긴 문서에서 실패)."""
        pii_step = config.COMBINED_GPU_PII_WINDOWS_PER_REQUEST
        injection_step = config.COMBINED_GPU_INJECTION_RECORDS_PER_REQUEST
        rounds = max(
            -(-len(pii_texts) // pii_step), -(-len(injection_records) // injection_step), 1
        )
        pii_entities: list[list[dict[str, Any]]] = []
        pii_kept: list[list[dict[str, Any]]] = []
        injection_results: list[dict[str, Any]] = []
        for index in range(rounds):
            pii_part = pii_texts[index * pii_step : (index + 1) * pii_step]
            injection_part = injection_records[index * injection_step : (index + 1) * injection_step]
            if pii_part and injection_part:
                payload = {"command": "parallel", "pii_texts": pii_part, "injection_records": injection_part}
            elif pii_part:
                payload = {"command": "pii", "texts": pii_part}
            elif injection_part:
                payload = {"command": "injection", "records": injection_part}
            else:
                continue
            if pii_part and include_pii_keep:
                payload["include_pii_keep"] = True
            response = await self._request(payload)
            pii_rows = response.get("pii_entities", []) if pii_part else []
            keep_rows = response.get("pii_kept", []) if pii_part and include_pii_keep else [[] for _ in pii_part]
            injection_rows = response.get("injection_results", []) if injection_part else []
            if (
                not isinstance(pii_rows, list)
                or not isinstance(keep_rows, list)
                or not isinstance(injection_rows, list)
                or len(pii_rows) != len(pii_part)
                or len(keep_rows) != len(pii_part)
                or len(injection_rows) != len(injection_part)
            ):
                await self.fail()
                raise CombinedGpuUnavailable("combined GPU worker returned malformed output")
            pii_entities += pii_rows
            pii_kept += keep_rows
            injection_results += injection_rows
        return pii_entities, pii_kept, injection_results

    async def _ocr_request(self, payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ocr_available:
            raise CombinedGpuUnavailable("combined GPU worker was started without OCR")
        response = await self._request(payload, timeout=timeout, ocr=True)
        if "error" in response:
            if response["error"] == "ocr_unavailable":
                raise CombinedGpuUnavailable("combined GPU worker was started without OCR")
            raise CombinedGpuOcrError(str(response["error"]))
        return response

    async def ocr_load(self, *, cuda_graph: bool) -> dict[str, Any]:
        """OCR 가중치를 worker 안에 올리고 사이드카 ready 와 같은 정보(device·batch·budget·graph·card_free_mb)를 돌려준다."""
        response = await self._ocr_request({"command": "ocr_load", "cuda_graph": cuda_graph},
                                           timeout=config.PII_LOAD_TIMEOUT_SEC)
        self.ocr_loaded = True
        return response

    async def ocr_read(self, image_b64: str, *, cuda_graph: bool) -> dict[str, Any]:
        """그림 한 장 → {"text", "tiles", "secs"} — 사이드카 응답의 results[0] 과 같다."""
        response = await self._ocr_request({"command": "ocr_read", "images": [image_b64], "cuda_graph": cuda_graph})
        self.ocr_loaded = True  # 내려가 있었으면 worker 가 읽으며 다시 올렸다
        return response["results"][0]

    async def ocr_unload(self) -> None:
        """OCR 가중치만 내린다 — 프로세스(PII·인젝션)는 남는다. 떠 있지 않으면 띄우지 않는다."""
        if self.ocr_loaded:
            with contextlib.suppress(CombinedGpuUnavailable):
                await self._request({"command": "ocr_unload"}, ocr=True, spawn=False)
        self.ocr_loaded = False

    def prewarm_background(self, on_unavailable=None) -> None:
        """앱 시작과 함께 띄운다. 쓸 수 없으면(CUDA 없음 등) on_unavailable 로 개별 sidecar 를
        대신 예열한다 — 그러지 않으면 GPU 없는 기기는 첫 검사에서 PII 모델을 처음 올린다."""
        if self._disabled:
            if on_unavailable is not None:
                self._prewarm_task = asyncio.create_task(on_unavailable())
            return
        if self._prewarm_task and not self._prewarm_task.done():
            return

        async def prewarm() -> None:
            try:
                await self.ensure_ready()
            except CombinedGpuUnavailable:
                if on_unavailable is not None:
                    await on_unavailable()

        self._prewarm_task = asyncio.create_task(prewarm())

    async def _idle_watcher(self) -> None:
        """유휴 시간이 지나면 프로세스를 내려 VRAM 을 돌려준다(개별 EXAONE sidecar 와 같은 정책).
        다음 요청이 다시 띄운다. 진행 중인 요청은 끊지 않는다."""
        timeout = config.INJECTION_LLM_IDLE_TIMEOUT_SEC
        if not timeout or timeout <= 0:
            return
        loop = asyncio.get_running_loop()
        while self._process is not None and self._process.returncode is None:
            await asyncio.sleep(min(30.0, timeout))
            if loop.time() - self._last_used < timeout:
                continue
            async with self._request_lock:
                if self._process is not None and loop.time() - self._last_used >= timeout:
                    await self.stop(cancel_idle=False)
            return

    async def fail(self) -> None:
        """이번 요청은 개별 sidecar 로 다시 하고, 연속 실패가 쌓였으면 끈다."""
        await self.stop()
        self._note_failure()

    async def disable(self) -> None:
        """현재 앱 실행 동안 통합 경로를 끄고 개별 sidecar 폴백을 사용한다."""
        await self.stop()
        self._disabled = True

    def _drop_process(self) -> None:
        proc = self._process
        self._process = None
        self._ready = False
        self.ocr_loaded = False
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.terminate()

    async def stop(self, *, cancel_idle: bool = True) -> None:
        if cancel_idle and self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        proc = self._process
        self._process = None
        self._ready = False
        self.ocr_loaded = False
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        if proc.returncode is None:
            proc.kill()

    def terminate_now(self) -> None:
        for task in (self._prewarm_task, self._idle_task):
            if task is not None and not task.done():
                task.cancel()
        self._drop_process()


_worker: CombinedGpuClient | None = None
_worker_loop: asyncio.AbstractEventLoop | None = None


def get_worker() -> CombinedGpuClient:
    global _worker, _worker_loop
    loop = asyncio.get_running_loop()
    if _worker is None or _worker_loop is not loop:
        if _worker is not None:
            _worker.terminate_now()
        _worker = CombinedGpuClient()
        _worker_loop = loop
    return _worker


def reset_worker() -> None:
    global _worker, _worker_loop
    if _worker is not None:
        _worker.terminate_now()
    _worker = None
    _worker_loop = None
