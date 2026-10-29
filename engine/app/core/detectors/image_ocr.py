"""
app/core/detectors/image_ocr.py
이미지 속 글자 검사 (선택 기능, config.IMAGE_OCR_ENABLED).

파서가 본문에 남긴 이미지 표시(parser/images.py)를 VARCO-VISION-2.0-1.7B 로 읽은 글로 바꾼다 — 그 뒤의 PII·인젝션·
기밀 탐지가 다른 글과 함께 한 번에 판정하고, 이미지 파일(화면 캡처)도 다른 문서처럼 가린 글로 내보낸다(사용자 결정).
- 사이드카(models/varco_ocr/runtime/varco_ocr_inference.py)는 이미지가 든 입력에서만 띄우고, 쓰지 않으면 내린다.
- 지원 GPU(CUDA·Intel XPU·Apple MPS)가 없거나 모델·실행 환경이 없어 글자 검사 대상 이미지를 못 읽으면
  'unchecked' — orchestrator 가 입력을 막는다.
- 작은 이미지(아이콘·로고)와 열 수 없는 그림 형식(EMF 등)은 글자 검사 대상이 아니라 건너뛴다.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import logging
import os
import time
from collections import OrderedDict
from typing import Any

from app import config
from app.core import model_status
from app.core.parser.images import MARK_RE

from .gpu_residency import GpuResidency
from .pii.encoder import EncoderPiiDetector

UNCHECKED_REASON = "이미지 속 글자를 검사하지 못해 보내지 않습니다 — GPU 실행 환경이 없거나 이미지 검사 모델이 준비되지 않았습니다"
# 모델 파일·GPU 는 있는데 사이드카가 죽은 경우. v0.2.56 Windows 실측(EN-6): 15GB PC 에서 VARCO(약 5.5GB)를 띄우다
# 가상 메모리(커밋)가 바닥나 사이드카가 죽었는데, 오류를 로그 없이 삼키고 위의 "GPU 없음" 문구를 붙여 원인을 못 찾았다.
LAUNCH_FAILED_REASON = "이미지 속 글자를 검사하지 못해 보내지 않습니다 — 이미지 검사 모델을 띄우지 못했습니다(PC 메모리가 부족할 수 있습니다)"
READ_FAILED_REASON = "이미지 속 글자를 검사하지 못해 보내지 않습니다 — 이미지 검사 모델이 그림을 읽다 멈췄습니다(PC 메모리가 부족할 수 있습니다)"
LOW_MEMORY_REASON = "이미지 속 글자를 검사하지 못해 보내지 않습니다 — PC 메모리가 부족해 이미지 검사 모델을 쓰지 않았습니다(다른 프로그램을 닫고 다시 보내 주세요)"

logger = logging.getLogger("securedoc.engine.image_ocr")


def free_commit_mb() -> float | None:
    """Windows 남은 시스템 커밋(MB, 작업 관리자 '커밋됨' 의 남은 양). 커밋 한도를 넘으면 프로그램이 죽는다.
    다른 OS(스왑이 자라 커밋 한도가 없다)·모르면 None."""
    if os.name != "nt":
        return None
    import ctypes

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
            (n, ctypes.c_ulonglong) for n in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                                              "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]

    status = _MemoryStatus(dwLength=ctypes.sizeof(_MemoryStatus))
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return status.ullAvailPageFile / 2**20


class VarcoOcrProcess(EncoderPiiDetector):
    """VARCO OCR 사이드카. 하위 프로세스 관리(_spawn_process·_rpc·죽으면 다시 띄우기)는 PII 탐지기 것을 그대로 쓴다."""

    name = "image_ocr"
    kind = "ocr"

    def __init__(self) -> None:  # 부모 __init__ 은 만들자마자 띄운다 — VARCO(가중치 약 4.1GB)는 이미지가 든 입력에서만
        self.residency = GpuResidency(model_name=self.name, mode="idle_unload",
                                      idle_timeout_sec=config.IMAGE_OCR_IDLE_UNLOAD_SEC, load_delay_sec=0.0)
        self._process: asyncio.subprocess.Process | None = None
        self._process_ready = False
        self._proc_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._next_id = 0
        self.no_gpu = False  # 사이드카가 GPU 를 못 찾았다 — 다시 띄우지 않는다(매번 띄워 보며 시간을 쓰지 않게)
        self.last_failure: str | None = None  # 마지막 read() 가 None 을 준 까닭(없으면 GPU·모델 미준비)
        self._idle_task: asyncio.Task | None = None

    def _spawn_args(self) -> list[str]:
        return [
            config.IMAGE_OCR_PYTHON_EXECUTABLE,
            str(config.IMAGE_OCR_ENGINE_DIR / "runtime" / "varco_ocr_inference.py"),
            "--model-dir", str(config.IMAGE_OCR_ENGINE_DIR / "models"),
            "--batch", str(config.IMAGE_OCR_BATCH),
            "--token-budget", str(config.IMAGE_OCR_TOKEN_BUDGET),
        ]

    def _spawn_env(self) -> dict[str, str]:
        """Windows는 별도 CUDA/XPU torch를 앞세우고, Apple Silicon은 번들 torch의 MPS를 쓴다."""
        if config.IMAGE_OCR_PYTHON_OVERRIDE:
            return {}
        if model_status.image_ocr_accelerator() == "mps":
            # 일부 비전 연산이 MPS 커널을 제공하지 않아도 해당 연산만 CPU로 내려 전체 요청을 실패시키지 않는다.
            return {"PYTORCH_ENABLE_MPS_FALLBACK": "1"}
        return {
            "PYTHONPATH": os.pathsep.join(
                filter(None, [str(config.IMAGE_OCR_GPU_TORCH_DIR), os.environ.get("PYTHONPATH")])
            )
        }

    async def _warmup(self) -> None:
        return None

    async def detect(self, text: str, *, meta=None):  # noqa: ARG002 - Detector 가 아니다
        raise NotImplementedError

    async def read(self, blob: bytes) -> dict[str, Any] | None:
        """이미지 한 장 → {"text", "tiles", "secs"}. 못 읽으면 None (GPU 없음·모델 없음·실패).
        한 요청에 한 장 — 응답 대기 상한(DETECT_INFER_TIMEOUT_SEC)이 요청 단위라 여러 장을 묶으면 넘는다."""
        self.last_failure = None
        if self.no_gpu or not model_status.image_ocr_ready():
            return None
        if not await self._enough_memory():
            self.last_failure = LOW_MEMORY_REASON
            return None
        try:
            await self._ensure_process()
        except RuntimeError as exc:
            logger.warning("[image_ocr] 사이드카를 띄우지 못함: %s", exc)
            if "no_gpu" in str(exc) or "no_accelerator" in str(exc):
                self.no_gpu = True
            else:
                self.last_failure = LAUNCH_FAILED_REASON
            return None
        try:
            result = await self._rpc({"images": [base64.b64encode(blob).decode("ascii")]})
            return result["results"][0]
        except Exception as exc:  # noqa: BLE001 - 한 장을 못 읽으면 그 이미지만 미검사로 둔다(입력이 막힌다)
            logger.warning("[image_ocr] 그림을 읽지 못함: %s", exc)
            self.last_failure = READ_FAILED_REASON
            return None
        finally:
            self.residency.touch()
            self._arm_unload()

    async def _enough_memory(self) -> bool:
        """남은 시스템 커밋이 모자라면 띄우지 않는다 / 떠 있으면 읽지 않고 내려 메모리를 돌려준다 — 메모리 작은 PC 가
        통째로 멎지 않게(v0.2.62 측정: 사이드카 커밋 8GB 로 PC 남은 커밋 0.67GB). 그림은 미검사로 막힌다(안전 쪽)."""
        free = free_commit_mb()
        running = self._process_ready
        need = config.IMAGE_OCR_READ_FREE_COMMIT_MB if running else config.IMAGE_OCR_LAUNCH_FREE_COMMIT_MB
        if free is None or free >= need:
            return True
        logger.warning("[image_ocr] 남은 시스템 메모리(커밋) %.0fMB < %dMB — %s", free, need,
                       "읽지 않고 사이드카를 내림" if running else "사이드카를 띄우지 않음")
        if running:
            async with self._request_lock:  # 다른 업로드가 읽는 중이면 끝난 뒤 내린다
                await self._kill_process()
                self._process_ready = False
                self.residency.state = "unloaded"
        return False

    def _arm_unload(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
        with contextlib.suppress(RuntimeError):
            self._idle_task = asyncio.get_running_loop().create_task(self._unload_when_idle())

    async def _unload_when_idle(self) -> None:
        await asyncio.sleep(config.IMAGE_OCR_IDLE_UNLOAD_SEC)
        async with self._request_lock:  # 읽는 중이면 끝날 때까지 기다린다
            await self._kill_process()
            self._process_ready = False
            self.residency.state = "unloaded"

    async def close(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
        await self._kill_process()
        self._process_ready = False


_process: VarcoOcrProcess | None = None


def get() -> VarcoOcrProcess:
    global _process
    if _process is None:
        _process = VarcoOcrProcess()
    return _process


def status() -> dict[str, Any]:
    """/health·모델 상태용."""
    return {
        "enabled": config.IMAGE_OCR_ENABLED,
        "ready": model_status.image_ocr_ready(),
        "gpu": model_status.image_ocr_gpu(),  # 기존 API 이름: CUDA·Intel XPU·Apple MPS 중 하나가 보이는가
        "accelerator": model_status.image_ocr_accelerator(),
        "noGpu": _process.no_gpu if _process is not None else None,  # 사이드카가 가속기를 못 썼다 — 한 번 띄워 봐야 안다
    }


# 업로드 사이 OCR 결과 캐시(그림 해시 → 읽은 결과). 그림 든 문서가 OCR 을 기다리다 사이트 업로드 시간이 지나면(맥 v0.2.60:
# Claude "업로드 시간 초과") 다시 붙이거나 같은 문서를 다른 사이트에 올릴 때 다시 읽지 않는다. 읽은 글은 원문이라 메모리에만
# 30분 둔다 — 프록시가 올린 첨부 원문을 들고 있는 시간(_PENDING_TTL_S)과 같다. 못 읽은 결과(None)는 담지 않는다.
_CACHE_TTL_SEC = 1800.0
_CACHE_MAX = 64
_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


def _cached(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None or hit[0] < time.monotonic():
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return {**hit[1], "secs": 0.0}  # 이번엔 읽지 않았다


def _remember(key: str, got: dict[str, Any]) -> None:
    _cache[key] = (time.monotonic() + _CACHE_TTL_SEC, got)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def _target(blob: bytes) -> tuple[bool, str | None]:
    """(글자 검사 대상인가, 건너뛰는 이유)."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(blob)) as im:
            w, h = im.size
    except (UnidentifiedImageError, OSError, ValueError):
        return False, "열 수 없는 그림 형식"  # EMF·WMF 같은 벡터 그림 — 지금까지처럼 검사하지 않는다
    if w < config.IMAGE_OCR_MIN_WIDTH or h < config.IMAGE_OCR_MIN_HEIGHT:
        return False, "작은 그림(아이콘·로고)"
    return True, None


async def fill(text: str, images: list[bytes], emit=None) -> tuple[str, list[dict[str, Any]]]:
    """본문의 이미지 표시를 읽은 글로 바꾼다. 돌려주는 값: (글, 이미지별 정보).

    정보: {"index", "status": read|skipped|unchecked, "reason", "start", "end", "secs"} — start/end 는 바꾼 뒤 글 기준.
    같은 이미지(같은 해시)는 한 번만 읽는다 — 30분 안에 다른 업로드에서 읽은 그림도 다시 읽지 않는다(_cache).
    """
    ocr = get()
    infos: list[dict[str, Any]] = []
    read_cache: dict[str, dict[str, Any] | None] = {}
    failed_why: dict[str, str | None] = {}
    targets = 0
    for i, blob in enumerate(images):
        ok, why = _target(blob)
        info: dict[str, Any] = {"index": i, "status": "skipped", "reason": why}
        if ok:
            targets += 1
            key = hashlib.sha1(blob).hexdigest()
            if key not in read_cache:
                if targets > config.IMAGE_OCR_MAX_IMAGES:
                    read_cache[key] = None
                elif (hit := _cached(key)) is not None:
                    read_cache[key] = hit
                else:
                    if emit:
                        await emit({"type": "step", "step": 1, "label": f"이미지 속 글자 읽는 중 ({targets}번째)..."})
                    read_cache[key] = await ocr.read(blob)
                    failed_why[key] = getattr(ocr, "last_failure", None)
                    if read_cache[key] is not None:
                        _remember(key, read_cache[key])
            got = read_cache[key]
            if got is None and failed_why.get(key) == LOW_MEMORY_REASON:
                # PC 메모리가 모자라 읽지 않은 그림은 빼고 글만 가려 보낸다 — 이미지 검사 끔과 같은 길(orchestrator imagesRemoved,
                # 사용자 결정 (가)). 그림이 나가지 않으니 그림 속 PII 도 나가지 않는다.
                info.update(status="removed", reason=LOW_MEMORY_REASON)
            elif got is None:
                info.update(status="unchecked", reason=(failed_why.get(key) or UNCHECKED_REASON)
                            if targets <= config.IMAGE_OCR_MAX_IMAGES
                            else f"이미지가 {config.IMAGE_OCR_MAX_IMAGES}장을 넘어 나머지는 검사하지 못했습니다")
            else:
                info.update(status="read", reason=None, ocr=got)
        infos.append(info)

    out, pos, last = [], 0, 0
    for m in MARK_RE.finditer(text):
        out.append(text[last:m.start()])
        pos += m.start() - last
        last = m.end()
        info = infos[int(m.group(1))]
        got = info.pop("ocr", None) if "ocr" in info else None
        if got is None or not got["text"].strip():
            info.setdefault("start", pos)
            info.setdefault("end", pos)
            continue
        head = f"[이미지 {info['index'] + 1}]\n"
        info["start"], info["end"] = pos, pos + len(head) + len(got["text"])
        info["secs"] = got.get("secs")
        out.append(head + got["text"])
        pos = info["end"]
    out.append(text[last:])
    for info in infos:
        info.pop("ocr", None)
    return "".join(out), infos
