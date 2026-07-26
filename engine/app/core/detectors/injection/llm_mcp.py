"""
app/core/detectors/injection/llm_mcp.py
인젝션 LLM+MCP 슬롯 실 구현 — EXAONE-3.5-2.4B-Instruct 백본 + regularized MLP
분류기(hwan님 GPU 서버(123.37.28.197)에서 학습, injection_exaone_regularized_mlp_engine
번들을 app/models/injection_engine/ 에 이식). AlignSentinel 논문의 attention-feature
기반 간접 인젝션 탐지 방식을 그대로 쓴다(pooled Enc-first: Acc 96.56% / FPR 1.72% /
FNR 1.41%, hwan님 서버 평가 기준).

로컬 서브프로세스(runtime/local_injection_inference.py --stdio)를 GPU 상주 정책의
"로드"에 대응시킨다:
    - ensure_loaded 역할(_ensure_process): 서브프로세스가 없거나 죽어 있으면 새로
      띄우고 ready 라인을 기다린다 — fail-closed 로 실제 모델 로딩이 끝날 때까지
      대기한 뒤에만 검사를 진행한다(스텁 시절의 가짜 sleep 이 아니라 진짜 콜드 스타트).
    - touch(): 마지막 사용 시각 갱신.
    - 유휴 워처(백그라운드 태스크): idle_timeout_sec 이 지나면 서브프로세스를 실제로
      종료해 VRAM 을 회수한다(PLAN §4.1 idle_unload 를 진짜로 구현 — 스텁은 상태
      플래그만 흉내냈지만 지금은 진짜 프로세스를 죽이고 다음 요청에서 재기동한다).

입력 형식 불일치: 이 분류기는 원래 system_prompt/user_prompt/tool_response 3필드가
필요하다(간접 인젝션: 에이전트가 도구 응답을 받는 상황을 전제). gateway 파이프라인은
문서 청크 텍스트 한 덩어리만 주므로, 청크 텍스트를 tool_response 에 넣고
system_prompt/user_prompt 는 "문서 검토/요약"이라는 gateway 실사용 맥락을 반영한
고정 placeholder 를 쓴다(config.INJECTION_LLM_SYSTEM_PROMPT/USER_PROMPT). 논문이
가정한 시나리오와 완전히 같지는 않다는 점을 유의 — 실제 문서로 검증 필요.

출력 매핑: 3-class {misaligned, aligned, non_instruction} 중 misaligned 만 인젝션
positive 로 보고, gateway 의 세부 인젝션 타입(7종)까지는 구분 못 하므로
OTHER_INJECTION 으로 매핑한다. 스팬 정보가 없는 청크 단위 분류라 Detection 의
start/end 는 청크 전체 범위로 채운다.

에러 처리: 토큰화/스팬 탐색 실패(예: 청크가 max_seq_len 을 넘거나 특수 토큰이
섞여 tool_response 서브스트링을 못 찾는 경우) 시 해당 청크만 미탐지로 넘어간다
(전체 요청을 실패시키지 않음) — 파이프라인의 다른 청크/detector 는 계속 동작한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any

from app import config

from ..base import ChunkMeta, Detection
from ..gpu_residency import GpuResidency


class InjectionLlmMcpDetector:
    """EXAONE-3.5-2.4B-Instruct + regularized MLP 인젝션 탐지기 — Detector Protocol 구현체."""

    name = "injection_llm_mcp"
    kind = "injection"

    def __init__(self) -> None:
        self.residency = GpuResidency(
            model_name=self.name,
            mode="idle_unload",
            idle_timeout_sec=config.INJECTION_LLM_IDLE_TIMEOUT_SEC,
            load_delay_sec=config.INJECTION_LLM_LOAD_DELAY_SEC,
        )
        self._process: asyncio.subprocess.Process | None = None
        self._proc_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()  # stdio 프로토콜은 요청을 1개씩 직렬 처리
        self._next_id = 0
        self._watcher_task: asyncio.Task | None = None

    def _runtime_script(self) -> Path:
        return config.INJECTION_ENGINE_DIR / "runtime" / "local_injection_inference.py"

    async def _spawn_process(self) -> None:
        script = self._runtime_script()
        args = [
            config.INJECTION_PYTHON_EXECUTABLE,
            str(script),
            "--engine-dir", str(config.INJECTION_ENGINE_DIR),
            "--backend-key", config.INJECTION_BACKEND_KEY,
            "--variant", config.INJECTION_VARIANT,
            "--detector-name", config.INJECTION_DETECTOR_NAME,
            "--device", config.INJECTION_DEVICE,
            "--device-map", config.INJECTION_DEVICE_MAP,
            "--dtype", config.INJECTION_DTYPE,
            "--max-seq-len", str(config.INJECTION_MAX_SEQ_LEN),
            "--stdio",
        ]
        # Windows(특히 한국어 로케일)에서 자식 프로세스의 sys.stdin/stdout 이 기본
        # 콘솔 코드페이지(cp949)로 열려, UTF-8 JSONL 요청의 한글이 깨지는 경우가
        # 있다(로컬 실측, pii/encoder.py 와 동일 이슈). PYTHONUTF8=1 로 자식
        # 프로세스의 텍스트 스트림을 강제로 UTF-8 로 연다.
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert self._process.stdout is not None
        payload = await self._read_ready_line(config.INJECTION_LLM_LOAD_DELAY_SEC, "injection_llm_mcp")
        if not payload.get("ready"):
            raise RuntimeError(f"injection_llm_mcp: 예상치 못한 준비 응답: {payload!r}")

    async def _read_ready_line(self, timeout_sec: float, tag: str) -> dict[str, Any]:
        """준비(ready) 라인을 기다린다.

        transformers 의 @auto_docstring 이 모델 클래스 임포트 시점에 "[ERROR] ...
        is part of ...'s signature, but not documented" 같은 진단 메시지를 stdout 에
        직접 print() 해서(로컬 실측) JSONL 프로토콜을 오염시키는 경우가 있다 — 이런
        비-JSON 잡음 라인은 건너뛰고, JSON 으로 파싱되는 첫 줄을 준비 응답으로 삼는다.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        assert self._process is not None and self._process.stdout is not None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._kill_process()
                raise RuntimeError(f"{tag}: 서브프로세스 로딩이 {timeout_sec}초 안에 끝나지 않음")
            try:
                line = await asyncio.wait_for(self._process.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                await self._kill_process()
                raise RuntimeError(f"{tag}: 서브프로세스 로딩이 {timeout_sec}초 안에 끝나지 않음") from None
            if not line:
                stderr = b""
                if self._process.stderr is not None:
                    stderr = await self._process.stderr.read()
                await self._kill_process()
                raise RuntimeError(f"{tag}: 서브프로세스가 준비 전 종료됨: {stderr.decode(errors='replace')[-2000:]}")
            try:
                return json.loads(line.decode())
            except json.JSONDecodeError:
                continue  # 진단 잡음 라인 — 무시하고 다음 줄 대기

    async def _kill_process(self) -> None:
        proc = self._process
        self._process = None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        if proc.returncode is None:
            proc.kill()

    async def _ensure_process(self) -> None:
        _ = self.residency.status  # idle 만료 체크를 트리거(내부에서 상태 갱신)
        alive = self._process is not None and self._process.returncode is None
        if self.residency.state == "loaded" and alive:
            return
        async with self._proc_lock:
            # 락 대기 중 다른 요청이 이미 로드를 끝냈을 수 있으므로 재확인.
            _ = self.residency.status
            alive = self._process is not None and self._process.returncode is None
            if self.residency.state == "loaded" and alive:
                return
            await self._kill_process()
            self.residency.state = "loading"
            await self._spawn_process()
            self.residency.mark_loaded_immediately()
            if self._watcher_task is None or self._watcher_task.done():
                self._watcher_task = asyncio.create_task(self._idle_watcher())

    async def _idle_watcher(self) -> None:
        """유휴 타임아웃이 지나면 실제로 서브프로세스를 죽여 VRAM 을 회수한다."""
        timeout = self.residency.idle_timeout_sec
        if timeout is None:
            return
        while True:
            await asyncio.sleep(min(30.0, timeout))
            _ = self.residency.status  # 만료 체크(내부에서 state 갱신)
            if self.residency.state != "loaded":
                async with self._proc_lock:
                    await self._kill_process()
                return
            if self._process is None or self._process.returncode is not None:
                self.residency.state = "unloaded"
                return

    async def _infer(self, text: str) -> dict[str, Any]:
        request = {
            "id": str(self._next_id),
            "system_prompt": config.INJECTION_LLM_SYSTEM_PROMPT,
            "user_prompt": config.INJECTION_LLM_USER_PROMPT,
            "tool_response": text,
        }
        self._next_id += 1
        async with self._request_lock:
            proc = self._process
            assert proc is not None and proc.stdin is not None and proc.stdout is not None
            line = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
            proc.stdin.write(line)
            await proc.stdin.drain()
            while True:
                out_line = await proc.stdout.readline()
                if not out_line:
                    stderr = b""
                    if proc.stderr is not None:
                        stderr = await proc.stderr.read()
                    raise RuntimeError(
                        f"injection_llm_mcp: 서브프로세스가 응답 없이 종료됨: {stderr.decode(errors='replace')[-2000:]}"
                    )
                try:
                    return json.loads(out_line.decode())
                except json.JSONDecodeError:
                    continue  # transformers 진단 잡음 라인 — 무시하고 다음 줄 대기

    async def detect(self, text: str, *, meta: ChunkMeta | None = None) -> list[Detection]:
        if not text.strip():
            return []
        await self._ensure_process()
        try:
            result = await self._infer(text)
        finally:
            self.residency.touch()

        if "error" in result:
            # 토큰화/스팬 탐색 실패 등 — 해당 청크만 미탐지로 넘어간다(전체 요청은
            # 실패시키지 않음). 원인은 서브프로세스 stderr 로그에서 확인 가능.
            return []
        if not result.get("is_injection"):
            return []

        scores = result.get("scores", {})
        confidence = float(scores.get("misaligned", 0.0))
        return [
            Detection(
                type="OTHER_INJECTION",
                start=0,
                end=len(text),
                text=text,
                confidence=confidence,
                source="llm",
            )
        ]


def build() -> InjectionLlmMcpDetector:
    return InjectionLlmMcpDetector()
