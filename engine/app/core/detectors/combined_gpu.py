"""Client for the single-process, two-stream CUDA inference worker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

from app import config
from app.core.detectors.injection import backbone


class CombinedGpuUnavailable(RuntimeError):
    """The combined CUDA path cannot be used; callers should use sidecars."""


class CombinedGpuClient:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._ready = False
        self._disabled = not config.COMBINED_GPU_WORKER_ENABLED
        self._process_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._next_id = 0
        self._prewarm_task: asyncio.Task[None] | None = None

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
        self._process = await asyncio.create_subprocess_exec(
            *self._args(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        payload = await self._read_response(
            config.COMBINED_GPU_LOAD_TIMEOUT_SEC, expected_id=None
        )
        if not payload.get("ready") or payload.get("device") != "cuda":
            raise CombinedGpuUnavailable(
                f"combined GPU worker unavailable: {payload.get('error', payload)!s}"
            )
        self._ready = True

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
            except BaseException as exc:
                await self.stop()
                self._disabled = True
                if isinstance(exc, asyncio.CancelledError):
                    raise
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
            assert proc is not None and proc.stdin is not None
            try:
                proc.stdin.write(
                    (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
                )
                await proc.stdin.drain()
                response = await self._read_response(
                    config.DETECT_INFER_TIMEOUT_SEC, expected_id=request_id
                )
            except BaseException as exc:
                await self.stop()
                self._disabled = True
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, CombinedGpuUnavailable):
                    raise
                raise CombinedGpuUnavailable(str(exc)) from exc
        if "error" in response:
            await self.stop()
            self._disabled = True
            raise CombinedGpuUnavailable(
                f"combined GPU inference failed: {response['error']}"
            )
        return response

    async def infer_parallel(
        self,
        pii_texts: list[str],
        injection_records: list[dict[str, str]],
    ) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
        response = await self._request(
            {
                "command": "parallel",
                "pii_texts": pii_texts,
                "injection_records": injection_records,
            }
        )
        pii_entities = response.get("pii_entities")
        injection_results = response.get("injection_results")
        malformed = (
            not isinstance(pii_entities, list)
            or not isinstance(injection_results, list)
            or len(pii_entities) != len(pii_texts)
            or len(injection_results) != len(injection_records)
        )
        if malformed:
            await self.stop()
            self._disabled = True
            raise CombinedGpuUnavailable("combined GPU worker returned malformed output")
        return pii_entities, injection_results

    def prewarm_background(self) -> None:
        if self._disabled or (self._prewarm_task and not self._prewarm_task.done()):
            return

        async def prewarm() -> None:
            with contextlib.suppress(CombinedGpuUnavailable):
                await self.ensure_ready()

        self._prewarm_task = asyncio.create_task(prewarm())

    async def disable(self) -> None:
        """현재 앱 실행 동안 통합 경로를 끄고 개별 sidecar 폴백을 사용한다."""
        await self.stop()
        self._disabled = True

    async def stop(self) -> None:
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
        if self._prewarm_task is not None and not self._prewarm_task.done():
            self._prewarm_task.cancel()
        proc = self._process
        self._process = None
        self._ready = False
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.terminate()


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
