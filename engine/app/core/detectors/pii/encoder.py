"""
app/core/detectors/pii/encoder.py
PII 인코더 슬롯 실 구현 — A.X-Encoder + Gazetteer + CRF + 문자 경계 MLP + 개체별
MASK/KEEP head (pii_direct_char_boundary_seed42 번들, config.PII_MODEL_SEED).
번들 검증(valid): 가려야 할 개체 5,660개 중 누출 67(1.18%), 남겨도 되는 858개 중
과잉 마스킹 1(0.12%), 정확 스팬·유형 F1 98.5%. 런타임이 KEEP 개체를 빼고 MASK 확률을
score 로 준다(6개 도메인 중 최댓값 — runtime/local_pii_inference.py 참고).

로컬 서브프로세스(runtime/local_pii_inference.py --stdio)를 GPU 상주
정책의 "always_on"에 대응시킨다:
    - CUDA가 있으면 CUDA, 없으면 CPU에서 돌고 dtype 은 항상 FP32 다(번들 검증 범위).
      앱 기동 시(build() 호출 시점에
      실행 중인 이벤트 루프가 있으면) 백그라운드로
      즉시 로딩을 시작한다(pre-warm). 이벤트 루프가 없으면(예: 단순 스크립트에서
      직접 build() 호출) 첫 detect() 호출 시 지연 로딩된다.
    - 로딩이 끝나기 전에 요청이 오면 fail-closed 로 로딩 완료까지 대기한 뒤
      검사한다(검사를 생략하지 않음).
    - always_on 이므로(GpuResidency.mode="always_on") idle 언로드는 없다 — 한 번
      뜬 서브프로세스는 앱이 끝날 때까지 계속 상주한다.

토큰 길이 제약: 이 모델의 권장 max_length 는 256 토큰인데(원 번들 기본값),
gateway 파이프라인은 원문 전체를 한 번에 넘긴다 — 그대로 넣으면 뒷부분이
잘려 후반부 PII 를 놓친다. detect() 내부에서 문자 기준 슬라이딩 윈도우
(PII_WINDOW_SIZE/PII_WINDOW_OVERLAP)로 청크를 잘라 한 번에 배치 추론하고,
윈도우 겹침으로 중복 탐지된 엔터티는 (type, start, end) 기준으로 both dedupe한다.

라벨 매핑: 런타임이 주는 KDPII 19종 코드(PS_NAME, QT_MOBILE, ...)를 gateway 의
PII_TYPES 상수(PERSON_NAME, PHONE, ...)로 매핑한다(다대일). 대응하는 gateway
상수가 없는 라벨(CV_POSITION, OGG_EDUCATION, QT_PLATE_NUMBER, QT_AGE, FD_MAJOR)은
OTHER_PII 로 묶는다.

신뢰도: 모델 개체는 MASK 확률(score), 정규식·주소 보정으로 더한 개체는 확률이 없어
고정값(_RULE_CONFIDENCE)을 준다. 신뢰도는 표시용이고 마스킹 여부를 바꾸지 않는다.
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

_RULE_CONFIDENCE = 0.9

# 모델 로딩 직후 첫 forward pass 가 CUDA 커널 선택/캐싱 1회성 비용을 떠안는
# 문제(injection_llm_mcp 와 동일 이슈, 실측: 콜당 ~0.3s → ~0.07s)를 피하려고,
# 실제 요청과 무관한 더미 텍스트로 로딩 단계에서 미리 한 번 태운다.
_WARMUP_TEXT = "오늘 날씨는 맑습니다."


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
        # always_on: residency.state 는 실제 서브프로세스 준비 여부와 무관하게 생성
        # 즉시 "loaded" 로 취급한다(GpuResidency 문서의 always_on 계약 — 상태 플래그는
        # 실제 GPU/프로세스를 다루지 않는 순수 표시용 fiction). 실제 준비는 아래
        # _process_ready 로 별도 추적되고, detect() 가 필요시 _ensure_process() 로
        # fail-closed 대기한다 — 이 둘을 혼동해 mark_loaded_immediately() 를 실제 웜업
        # 완료 시점에만 호출하면(과거 버그) "즉시 loaded" 계약이 깨진다.
        self.residency.mark_loaded_immediately()
        self._process: asyncio.subprocess.Process | None = None
        # _process(비어있지 않음)만으로는 "요청을 받을 준비가 됐다"를 보장 못 한다 —
        # create_subprocess_exec() 가 반환하는 즉시 _process 는 non-None 이 되지만,
        # 모델 로딩(_read_ready_line())은 그 뒤로도 한참(수 초~수십 초) 더 걸린다.
        # 예전엔 이 구간에서 residency.state(always_on 이라 항상 "loaded")와 _process
        # 존재 여부만으로 "이미 로드됨"을 판정해, 동시에 들어온 다른 detect() 호출이
        # _proc_lock 을 거치지 않고 곧장 _infer_batch() 로 진입해 같은 stdout 을
        # _read_ready_line() 과 동시에 읽어버렸다(실측: "readuntil() called while
        # another coroutine is already waiting for incoming data" — 여러 청크를
        # asyncio.gather 로 동시 탐지하는 콜드스타트/재기동 직후 재현됨). _process_ready
        # 는 _read_ready_line() 이 실제로 끝난 뒤에만 True 가 되는, residency.state 와
        # 완전히 분리된 진짜 준비 신호다.
        self._process_ready = False
        self._proc_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()  # stdio 프로토콜은 요청을 1개씩 직렬 처리
        self._next_id = 0
        with contextlib.suppress(RuntimeError):
            # 실행 중인 이벤트 루프가 있으면(정상적으로 lifespan 안에서 build() 호출된
            # 경우) 앱 기동 즉시 백그라운드 pre-warm 을 시작한다(always_on 취지).
            # 루프가 없으면(예: 스크립트에서 직접 build()) 첫 detect() 에서 지연 로딩.
            # 통합 GPU worker가 켜진 경우에는 그 worker가 PII 모델까지 함께 예열한다.
            # 여기서 개별 sidecar도 띄우면 같은 가중치를 GPU에 두 번 올리게 되므로,
            # 개별 sidecar는 통합 worker 실패 시 첫 detect()에서만 지연 기동한다.
            if not config.COMBINED_GPU_WORKER_ENABLED:
                asyncio.get_running_loop()
                asyncio.create_task(self._ensure_process())

    def _runtime_script(self) -> Path:
        return config.PII_ENGINE_DIR / "runtime" / "local_pii_inference.py"

    def _model_dir(self) -> Path:
        return config.PII_ENGINE_DIR / "models" / config.PII_MODEL_SEED

    def _spawn_args(self) -> list[str]:
        return [
            config.PII_PYTHON_EXECUTABLE,
            str(self._runtime_script()),
            "--model-dir", str(self._model_dir()),
            "--device", config.PII_DEVICE,
            "--dtype", config.PII_DTYPE,
            "--batch-size", str(config.PII_BATCH_SIZE),
            "--max-length", str(config.PII_MAX_LENGTH),
            "--stdio",
        ]

    async def _spawn_process(self) -> None:
        args = self._spawn_args()
        # Windows(특히 한국어 로케일)에서 자식 프로세스의 sys.stdin/stdout 이 기본
        # 콘솔 코드페이지(cp949)로 열려, UTF-8 JSONL 요청의 한글이 깨져 tokenizers
        # 가 "TextEncodeInput must be Union[...]" 로 실패하는 경우가 있다(로컬 실측).
        # PYTHONUTF8=1 로 자식 프로세스의 텍스트 스트림을 강제로 UTF-8 로 연다.
        # HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE=1 — 모델이 이미 로컬에 번들되어 있는데도
        # from_pretrained() 가 매번 HuggingFace Hub 에 업데이트 확인 요청을 보내던 것을
        # 생략한다(실측: 콜드스타트 로딩 시간 단축, 특히 인젝션 쪽에서 효과 큼).
        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=config.SUBPROCESS_STREAM_LIMIT,
        )
        assert self._process.stdout is not None
        payload = await self._read_ready_line(config.PII_LOAD_TIMEOUT_SEC, self.name)
        if not payload.get("ready"):
            raise RuntimeError(f"{self.name}: 예상치 못한 준비 응답: {payload!r}")
        self.residency.device = payload.get("device")

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
        if self._process_ready and alive:
            return
        async with self._proc_lock:
            alive = self._process is not None and self._process.returncode is None
            if self._process_ready and alive:
                return
            if alive:
                await self._kill_process()
            self._process_ready = False
            await self._spawn_process()
            await self._warmup()
            self._process_ready = True

    async def _warmup(self) -> None:
        """CUDA 커널 선택/캐싱 등 1회성 초기화 비용을 로딩 단계에서 미리 지불
        (best-effort — 실패해도 로딩 자체를 실패시키지 않는다)."""
        with contextlib.suppress(Exception):
            await self._infer_batch([_WARMUP_TEXT])

    async def _infer_batch(self, texts: list[str]) -> list[list[dict[str, Any]]]:
        return (await self._rpc({"texts": texts}))["entities"]

    async def _rpc(self, request: dict[str, Any]) -> dict[str, Any]:
        """요청 하나를 보내고 응답 하나를 받는다 (기밀 탐지 리트리버도 같은 프로토콜로 쓴다)."""
        request = {"id": str(self._next_id), **request}
        self._next_id += 1
        async with self._request_lock:
            proc = self._process
            assert proc is not None and proc.stdin is not None and proc.stdout is not None
            line = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
            proc.stdin.write(line)
            await proc.stdin.drain()
            # 응답 대기 상한 — 인젝션 쪽(_infer)과 같은 이유다. 이 대기는
            # _request_lock 안이라, 멎으면 뒤에 줄 선 청크가 전부 함께 멎는다.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + config.DETECT_INFER_TIMEOUT_SEC
            result = None
            while result is None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await self._kill_process()  # 멎은 프로세스는 버린다 — 다음 요청이 새로 띄운다
                    raise RuntimeError(
                        f"{self.name}: 서브프로세스가 {config.DETECT_INFER_TIMEOUT_SEC}초 안에 "
                        "응답하지 않음 — 프로세스를 재시작합니다"
                    )
                try:
                    out_line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue  # 다음 루프에서 remaining<=0 으로 걸려 위 분기로 간다
                if not out_line:
                    stderr = b""
                    if proc.stderr is not None:
                        stderr = await proc.stderr.read()
                    raise RuntimeError(
                        f"{self.name}: 서브프로세스가 응답 없이 종료됨: {stderr.decode(errors='replace')[-2000:]}"
                    )
                try:
                    result = json.loads(out_line.decode())
                except json.JSONDecodeError:
                    continue  # 진단 잡음 라인 — 무시하고 다음 줄 대기
        if "error" in result:
            raise RuntimeError(f"{self.name}: 추론 오류: {result['error']}")
        return result

    def prepare_windows(self, text: str) -> list[tuple[str, int]]:
        """통합 worker와 개별 sidecar가 동일한 400/60 윈도우를 쓰게 한다."""
        if not text.strip():
            return []
        return _split_windows(text, config.PII_WINDOW_SIZE, config.PII_WINDOW_OVERLAP)

    def decode_windows(
        self,
        text: str,
        windows: list[tuple[str, int]],
        entity_lists: list[list[dict[str, Any]]],
    ) -> list[Detection]:
        """worker의 윈도우별 BIO 결과를 원문 좌표 Detection으로 복원한다."""
        if len(entity_lists) != len(windows):
            raise RuntimeError("pii_encoder: 윈도우 응답 수가 요청 수와 다릅니다")
        seen: set[tuple[str, int, int]] = set()
        detections: list[Detection] = []
        for (_, win_offset), entities in zip(windows, entity_lists, strict=True):
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
                        confidence=float(entity.get("score", _RULE_CONFIDENCE)),
                        source="encoder",
                    )
                )
        return detections

    async def detect(self, text: str, *, meta: ChunkMeta | None = None) -> list[Detection]:
        windows = self.prepare_windows(text)
        if not windows:
            return []
        await self._ensure_process()
        texts = [window for window, _ in windows]
        entity_lists: list[list[dict[str, Any]]] = []
        try:
            # 요청마다 윈도우 수 상한 — 응답 한 줄 크기와 추론 시간 상한이 문서 길이에 비례해 커지지 않게.
            for start in range(0, len(texts), config.PII_WINDOWS_PER_RPC):
                entity_lists += await self._infer_batch(texts[start : start + config.PII_WINDOWS_PER_RPC])
        finally:
            self.residency.touch()
        return self.decode_windows(text, windows, entity_lists)


def build() -> EncoderPiiDetector:
    return EncoderPiiDetector()
