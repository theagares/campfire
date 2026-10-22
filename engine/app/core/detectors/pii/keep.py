"""
app/core/detectors/pii/keep.py
회사 연락처처럼 가릴 필요 없는 PII 에 "풀어도 됨(keep)" 표시를 붙인다 (선택 기능, config.PII_KEEP_ENABLED).

keep 모델(runtime/keep_scorer.py 하위 프로세스)이 PII 개체마다 MASK 확률을 매긴다. 이 표시는 확장 검토 화면이
해당 항목을 미리 풀어 두는 데만 쓴다 — maskedText 는 그대로 전부 가린다(프록시·MCP 는 사람이 보지 않는다).

- 풀어 줄 수 있는 종류는 config.PII_KEEP_LABELS(회사 연락처 계열)뿐이다.
- 점수가 없거나(모델이 같은 종류로 겹치는 개체를 못 찾음) 임계값 이상이면 그대로 가린다.
- 모델이 없거나 꺼져 있거나 실패하면 아무 표시도 붙이지 않는다(= 전부 가림).
"""

from __future__ import annotations

import asyncio
import logging

from app import config
from app.core import model_status

from ..base import Detection
from .encoder import EncoderPiiDetector

log = logging.getLogger(__name__)


class KeepProcess(EncoderPiiDetector):
    name = "pii_keep"
    kind = "pii"

    def _spawn_args(self) -> list[str]:
        return [
            config.PII_PYTHON_EXECUTABLE,
            str(config.PII_ENGINE_DIR / "runtime" / "keep_scorer.py"),
            "--keep-dir", str(config.PII_KEEP_DIR),
            "--pii-model-dir", str(config.PII_ENGINE_DIR / "models" / config.PII_MODEL_SEED),
            "--device", config.PII_KEEP_DEVICE,
            "--stdio",
        ]

    async def _warmup(self) -> None:
        return None

    async def detect(self, text: str, *, meta=None):  # noqa: D102 - 탐지기가 아니다
        raise NotImplementedError("keep 은 annotate() 로 쓴다")


_process: KeepProcess | None = None
_tasks: set[asyncio.Task] = set()


def prewarm() -> None:
    """켜져 있고 모델이 있으면 엔진 기동 때 미리 띄운다 — 첫 기동이 CPU 약 13초라 첫 프롬프트가 기다리지 않게.

    PII 모델이 다 뜬 뒤에 띄운다. 느린 CPU 에서 두 모델을 같이 올리면 PII 로딩이 시간 상한을 넘긴다
    (orchestrator 의 Intel mac 주석과 같은 이유).
    """
    if not (config.PII_KEEP_ENABLED and model_status.pii_keep_ready()):
        return

    async def run() -> None:
        global _process
        from .. import registry
        ensure_pii = getattr(registry.get_pii_detector(), "_ensure_process", None)
        if callable(ensure_pii) and not config.COMBINED_GPU_WORKER_ENABLED:
            await ensure_pii()
        if _process is None:
            _process = KeepProcess()
        await _process._ensure_process()

    def done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if not t.cancelled() and t.exception():
            log.warning("keep 미리 띄우기 실패(처음 쓸 때 다시 띄운다): %s", t.exception())

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(done)


async def annotate(text: str, items: list[Detection]) -> list[Detection]:
    """풀어 줄 수 있는 종류의 PII 에 maskProb(MASK 확률)와 keep(풀어도 됨)을 붙인다. items 를 제자리에서 고친다."""
    if not config.PII_KEEP_ENABLED or not model_status.pii_keep_ready() or len(text) > config.PII_KEEP_MAX_CHARS:
        return items
    targets = [it for it in items if it.get("source") == "encoder" and it.get("label") in config.PII_KEEP_LABELS]
    if not targets:
        return items
    global _process
    if _process is None:
        _process = KeepProcess()
    try:
        await _process._ensure_process()
        try:
            reply = await _process._rpc({
                "text": text,
                "entities": [{"begin": it["start"], "end": it["end"], "label": it["label"]} for it in targets],
            })
        finally:
            _process.residency.touch()
    except Exception:  # noqa: BLE001 - 실패하면 전부 가린다(표시 없음)
        log.exception("keep 점수 실패 — 전부 가립니다")
        return items
    threshold = float(reply["threshold"])
    for it, p in zip(targets, reply["probs"], strict=True):
        if p is None:
            continue
        it["maskProb"] = p
        it["keep"] = p < threshold
    return items
