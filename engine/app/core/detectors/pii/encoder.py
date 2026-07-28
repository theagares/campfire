"""
app/core/detectors/pii/encoder.py
PII 인코더 슬롯 실 구현 — skt/A.X-Encoder-base 백본 + CRF + gazetteer 단일 모델
(config.PII_MODEL_SEED, 기본 seed42). hwan님이 준비한
pii_skt_crf_gaz_mix_all_x3_local_app 번들을 app/models/pii_engine/ 에 이식.
평가 지표(실 라벨셋 PII_dataset, 3,524문장 기준): Precision 99.71% / Recall 96.13% /
F1 97.89%.

원래 seed42/43/44 3-seed 앙상블(동일 라벨·겹치는 스팬 과반 투표 min_votes=2)로
구동했으나, 번들의 세 seed 디렉터리가 실제로는 완전히 동일한 가중치(하드링크)로
패키징된 버그가 확인되어 앙상블의 다양성 이득이 전혀 없었다 — 연산량/VRAM 3배
낭비만 있고 실질 효과는 없었으므로 단일 모델로 전환하기로 결정.

로컬 서브프로세스(runtime/local_pii_inference.py --stdio)를 GPU 상주
정책의 "always_on"에 대응시킨다:
    - CPU 로 돌아가는 가벼운 모델이라(GPU 는 인젝션 LLM 전용으로 비워둔다) 앱
      기동 시(build() 호출 시점에 실행 중인 이벤트 루프가 있으면) 백그라운드로
      즉시 로딩을 시작한다(pre-warm). 이벤트 루프가 없으면(예: 단순 스크립트에서
      직접 build() 호출) 첫 detect() 호출 시 지연 로딩된다.
    - 로딩이 끝나기 전에 요청이 오면 fail-closed 로 로딩 완료까지 대기한 뒤
      검사한다(검사를 생략하지 않음).
    - always_on 이므로(GpuResidency.mode="always_on") idle 언로드는 없다 — 한 번
      뜬 서브프로세스는 앱이 끝날 때까지 계속 상주한다.

토큰 길이 제약: 이 모델의 권장 max_length 는 256 토큰인데(원 번들 기본값),
gateway 파이프라인은 1,500자 청크를 그대로 넘긴다 — 그대로 넣으면 뒷부분이
잘려 후반부 PII 를 놓친다. detect() 내부에서 문자 기준 슬라이딩 윈도우
(PII_WINDOW_SIZE/PII_WINDOW_OVERLAP)로 청크를 잘라 한 번에 배치 추론하고,
윈도우 겹침으로 중복 탐지된 엔터티는 (type, start, end) 기준으로 both dedupe한다.

라벨 매핑: 실 모델의 19종 BIO 엔터티 라벨(PS_NAME, QT_MOBILE, ...)을 gateway 의
PII_TYPES 상수(PERSON_NAME, PHONE, ...)로 매핑한다(다대일). 대응하는 gateway
상수가 없는 라벨(CV_POSITION, OGG_EDUCATION, QT_PLATE_NUMBER, QT_AGE, FD_MAJOR)은
OTHER_PII 로 묶는다.

신뢰도: 단일 모델(CRF decode)은 엔터티별 확률을 따로 주지 않으므로, 모든 탐지에
고정 신뢰도(_SINGLE_MODEL_CONFIDENCE)를 부여한다 — 실 라벨셋 기준 P 99.71%로
정밀도가 매우 높아 고정값으로도 과도한 과신은 아니라고 판단.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any

from app import config

from ..base import (
    ADDRESS,
    BANK_ACCOUNT,
    CREDIT_CARD,
    DATE_OF_BIRTH,
    EMAIL,
    ID_NUMBER,
    ORGANIZATION,
    OTHER_PII,
    PERSON_NAME,
    PHONE,
    ChunkMeta,
    Detection,
)
from ..gpu_residency import GpuResidency

# 실 모델 19종 라벨 → gateway PII_TYPES 매핑 (다대일).
LABEL_MAP: dict[str, str] = {
    "PS_NAME": PERSON_NAME,
    "LC_ADDRESS": ADDRESS,
    "OG_WORKPLACE": ORGANIZATION,
    "OG_DEPARTMENT": ORGANIZATION,
    "CV_POSITION": OTHER_PII,
    "OGG_EDUCATION": OTHER_PII,
    "QT_MOBILE": PHONE,
    "QT_PHONE": PHONE,
    "QT_RESIDENT_NUMBER": ID_NUMBER,
    "QT_ALIEN_NUMBER": ID_NUMBER,
    "QT_DRIVER_NUMBER": ID_NUMBER,
    "QT_PLATE_NUMBER": OTHER_PII,
    "QT_ACCOUNT_NUMBER": BANK_ACCOUNT,
    "QT_CARD_NUMBER": CREDIT_CARD,
    "TMI_EMAIL": EMAIL,
    "QT_PASSPORT_NUMBER": ID_NUMBER,
    "QT_AGE": OTHER_PII,
    "DT_BIRTH": DATE_OF_BIRTH,
    "FD_MAJOR": OTHER_PII,
}

_SINGLE_MODEL_CONFIDENCE = 0.9


def _split_windows(text: str, size: int, overlap: int) -> list[tuple[str, int]]:
    """문자 기준 슬라이딩 윈도우. (윈도우 텍스트, 청크 내 시작 오프셋) 리스트 반환."""
    if len(text) <= size:
        return [(text, 0)]
    step = max(1, size - overlap)
    windows: list[tuple[str, int]] = []
    offset = 0
    while offset < len(text):
        windows.append((text[offset : offset + size], offset))
        if offset + size >= len(text):
            break
        offset += step
    return windows


class EncoderPiiDetector:
    """skt/A.X-Encoder-base + CRF + gazetteer 단일 모델 PII 탐지기 — Detector Protocol 구현체."""

    name = "pii_encoder"
    kind = "pii"

    def __init__(self) -> None:
        self.residency = GpuResidency(
            model_name=self.name,
            mode="always_on",
            idle_timeout_sec=None,
            load_delay_sec=config.PII_LOAD_TIMEOUT_SEC,
        )
        self._process: asyncio.subprocess.Process | None = None
        self._proc_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()  # stdio 프로토콜은 요청을 1개씩 직렬 처리
        self._next_id = 0
        with contextlib.suppress(RuntimeError):
            # 실행 중인 이벤트 루프가 있으면(정상적으로 lifespan 안에서 build() 호출된
            # 경우) 앱 기동 즉시 백그라운드 pre-warm 을 시작한다(always_on 취지).
            # 루프가 없으면(예: 스크립트에서 직접 build()) 첫 detect() 에서 지연 로딩.
            asyncio.get_running_loop()
            asyncio.create_task(self._ensure_process())

    def _runtime_script(self) -> Path:
        return config.PII_ENGINE_DIR / "runtime" / "local_pii_inference.py"

    def _model_dir(self) -> Path:
        return config.PII_ENGINE_DIR / "models" / config.PII_MODEL_SEED

    async def _spawn_process(self) -> None:
        script = self._runtime_script()
        args = [
            config.PII_PYTHON_EXECUTABLE,
            str(script),
            "--model-dir", str(self._model_dir()),
            "--device", config.PII_DEVICE,
            "--batch-size", str(config.PII_BATCH_SIZE),
            "--max-length", str(config.PII_MAX_LENGTH),
            "--stdio",
        ]
        # Windows(특히 한국어 로케일)에서 자식 프로세스의 sys.stdin/stdout 이 기본
        # 콘솔 코드페이지(cp949)로 열려, UTF-8 JSONL 요청의 한글이 깨져 tokenizers
        # 가 "TextEncodeInput must be Union[...]" 로 실패하는 경우가 있다(로컬 실측).
        # PYTHONUTF8=1 로 자식 프로세스의 텍스트 스트림을 강제로 UTF-8 로 연다.
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert self._process.stdout is not None
        payload = await self._read_ready_line(config.PII_LOAD_TIMEOUT_SEC, "pii_encoder")
        if not payload.get("ready"):
            raise RuntimeError(f"pii_encoder: 예상치 못한 준비 응답: {payload!r}")

    async def _read_ready_line(self, timeout_sec: float, tag: str) -> dict[str, Any]:
        """준비(ready) 라인을 기다린다.

        transformers 의 @auto_docstring 등이 모델 클래스 임포트 시점에 진단
        메시지를 stdout 에 직접 print() 해서(injection_llm_mcp 에서 실측) JSONL
        프로토콜을 오염시키는 경우가 있다 — 이런 비-JSON 잡음 라인은 건너뛰고,
        JSON 으로 파싱되는 첫 줄을 준비 응답으로 삼는다.
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
        alive = self._process is not None and self._process.returncode is None
        if self.residency.state == "loaded" and alive:
            return
        async with self._proc_lock:
            alive = self._process is not None and self._process.returncode is None
            if self.residency.state == "loaded" and alive:
                return
            if alive:
                await self._kill_process()
            await self._spawn_process()
            self.residency.mark_loaded_immediately()

    async def _infer_batch(self, texts: list[str]) -> list[list[dict[str, Any]]]:
        request = {"id": str(self._next_id), "texts": texts}
        self._next_id += 1
        async with self._request_lock:
            proc = self._process
            assert proc is not None and proc.stdin is not None and proc.stdout is not None
            line = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
            proc.stdin.write(line)
            await proc.stdin.drain()
            result = None
            while result is None:
                out_line = await proc.stdout.readline()
                if not out_line:
                    stderr = b""
                    if proc.stderr is not None:
                        stderr = await proc.stderr.read()
                    raise RuntimeError(
                        f"pii_encoder: 서브프로세스가 응답 없이 종료됨: {stderr.decode(errors='replace')[-2000:]}"
                    )
                try:
                    result = json.loads(out_line.decode())
                except json.JSONDecodeError:
                    continue  # 진단 잡음 라인 — 무시하고 다음 줄 대기
        if "error" in result:
            raise RuntimeError(f"pii_encoder: 추론 오류: {result['error']}")
        return result["entities"]

    async def detect(self, text: str, *, meta: ChunkMeta | None = None) -> list[Detection]:
        if not text.strip():
            return []
        await self._ensure_process()
        try:
            windows = _split_windows(text, config.PII_WINDOW_SIZE, config.PII_WINDOW_OVERLAP)
            entity_lists = await self._infer_batch([w for w, _ in windows])
        finally:
            self.residency.touch()

        seen: set[tuple[str, int, int]] = set()
        detections: list[Detection] = []
        for (_, win_offset), entities in zip(windows, entity_lists):
            for entity in entities:
                gateway_type = LABEL_MAP.get(entity["label"], OTHER_PII)
                start = win_offset + int(entity["begin"])
                end = win_offset + int(entity["end"])
                key = (gateway_type, start, end)
                if key in seen:
                    continue
                seen.add(key)
                detections.append(
                    Detection(
                        type=gateway_type,
                        start=start,
                        end=end,
                        text=text[start:end],
                        confidence=_SINGLE_MODEL_CONFIDENCE,
                        source="encoder",
                    )
                )
        return detections


def build() -> EncoderPiiDetector:
    return EncoderPiiDetector()
