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


class CombinedGpuUnavailable(RuntimeError):
    """The combined CUDA path cannot be used; callers should use sidecars."""


class CombinedGpuNoCuda(CombinedGpuUnavailable):
    """이 기기에 CUDA 가 없다 — 앱을 다시 켜도 바뀌지 않으므로 이번 실행 동안 끈다."""


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

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.ensure_ready()
        request_id = str(self._next_id)
        self._next_id += 1
        request = {"id": request_id, **payload}
        async with self._request_lock:
            proc = self._process
            if proc is None:  # 대기하는 사이 유휴 해제됐다
                raise CombinedGpuUnavailable("combined GPU worker was unloaded")
            assert proc.stdin is not None
            self._last_used = asyncio.get_running_loop().time()
            try:
                proc.stdin.write(
                    (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
                )
                await proc.stdin.drain()
                response = await self._read_response(
                    config.DETECT_INFER_TIMEOUT_SEC, expected_id=request_id
                )
            except asyncio.CancelledError:
                # 응답을 읽다 만 프로세스는 다음 요청과 줄이 어긋난다 — 버리되, 끄지는 않는다.
                self._drop_process()
                raise
            except BaseException as exc:
                await self.stop()
                self._note_failure()
                if isinstance(exc, CombinedGpuUnavailable):
                    raise
                raise CombinedGpuUnavailable(str(exc)) from exc
            self._last_used = asyncio.get_running_loop().time()
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
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.terminate()

    async def stop(self, *, cancel_idle: bool = True) -> None:
        if cancel_idle and self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        proc = self._process
        self._process = None
        self._ready = False
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
