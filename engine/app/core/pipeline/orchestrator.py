"""
app/core/pipeline/orchestrator.py
파이프라인 오케스트레이션 (PLAN §2, §6):
    parse → chunk(1500자) → detect(pii, injection) → mask → wrap

- PII/인젝션 위치는 모두 "원문 기준" 좌표로 정규화해 반환한다(익스텐션 계약).
- 파싱 실패/미지원/타임아웃은 예외로 죽지 않고 scan_status 로 통과 처리(PLAN §9.2).
- 인젝션 정책 mask(기본): [인젝션 마스킹] 치환 후 통과 / block: blocked=True.
"""

from __future__ import annotations

import base64
from typing import Any, Awaitable, Callable

from app import config
from app.core.detectors import registry
from app.core.detectors.base import Detection
from app.core.masker import docwrapper, masker
from app.core.parser import STATUS_OK, parse_document

Emit = Callable[[dict], Awaitable[None]]


async def _noop_emit(_event: dict) -> None:
    return None


def _split_chunks(text: str, chunk_size: int, overlap: int = 100) -> list[dict]:
    """1,500자 청크 분할(+겹침). 경계에서 매치가 잘리는 것을 겹침으로 완화."""
    if not text:
        return [{"text": "", "offset": 0}]
    chunks: list[dict] = []
    step = max(1, chunk_size - overlap)
    i = 0
    n = len(text)
    while i < n:
        chunks.append({"text": text[i : i + chunk_size], "offset": i})
        if i + chunk_size >= n:
            break
        i += step
    return chunks


def _dedupe(items: list[Detection]) -> list[Detection]:
    """(type, start, end) 기준 중복 제거(청크 겹침으로 생긴 중복)."""
    seen: set[tuple] = set()
    out: list[Detection] = []
    for it in sorted(items, key=lambda x: (x["start"], x["end"], -x["confidence"])):
        key = (it["type"], it["start"], it["end"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


async def _detect_all(detector, text: str, chunks: list[dict]) -> list[Detection]:
    results: list[Detection] = []
    total = len(chunks)
    for idx, ch in enumerate(chunks):
        dets = await detector.detect(ch["text"], meta={"chunk_index": idx, "total_chunks": total, "offset": ch["offset"]})
        for d in dets:
            d["start"] += ch["offset"]
            d["end"] += ch["offset"]
            d["text"] = text[d["start"] : d["end"]]
            results.append(d)
    return _dedupe(results)


async def run_pipeline(
    *,
    text: str | None = None,
    file_bytes: bytes | None = None,
    mime_type: str = "",
    file_name: str = "prompt.txt",
    emit: Emit | None = None,
    wrap_file: bool = False,
) -> dict[str, Any]:
    """텍스트(prompt) 또는 파일 파이프라인 공통 실행기.

    text 가 주어지면 프롬프트 경로, file_bytes 가 주어지면 파일 경로.
    """
    emit = emit or _noop_emit
    scan_status = STATUS_OK
    reason: str | None = None

    # ── Step 1: 파싱 ──────────────────────────────────────────────────────────
    await emit({"type": "step", "step": 1, "label": "입력 파싱 중..."})
    if text is None:
        text, scan_status, reason = parse_document(file_bytes or b"", mime_type, file_name)
    await emit({"type": "step", "step": 1, "label": "파싱 완료", "done": True})

    # 미검사 통과 (PLAN §9.2): 파싱 실패/미지원이면 탐지 없이 통과
    if scan_status != STATUS_OK:
        await emit({"type": "warning", "scanStatus": scan_status, "reason": reason})
        return _build_result(
            original_text=text or "",
            masked_text=text or "",
            pii_items=[],
            injection_items=[],
            scan_status=scan_status,
            reason=reason,
            blocked=False,
            masked_file=None,
        )

    # ── Step 2~3: 청크 + PII 탐지 ─────────────────────────────────────────────
    chunks = _split_chunks(text, config.CHUNK_SIZE)
    await emit({"type": "step", "step": 2, "label": f"PII 탐지 중 (총 {len(chunks)}개 청크)..."})
    pii_items = await _detect_all(registry.get_pii_detector(), text, chunks)
    await emit({"type": "step", "step": 2, "label": f"PII 탐지 완료 ({len(pii_items)}개)", "done": True})

    # ── Step 4: 인젝션 탐지 ───────────────────────────────────────────────────
    await emit({"type": "step", "step": 4, "label": "인젝션 탐지 중..."})
    injection_items = await _detect_all(registry.get_injection_detector(), text, chunks)
    await emit({"type": "step", "step": 4, "label": f"인젝션 탐지 완료 ({len(injection_items)}개)", "done": True})

    # ── 정책: block 이면 인젝션 탐지 시 차단 ──────────────────────────────────
    blocked = bool(injection_items) and config.INJECTION_POLICY == "block"

    # ── Step 5: 마스킹 ────────────────────────────────────────────────────────
    await emit({"type": "step", "step": 5, "label": "마스킹 적용 중..."})
    masked = masker.apply_masking(text, list(pii_items) + list(injection_items))
    masked_text = masked["masked_text"]

    masked_file = None
    if wrap_file and not blocked:
        wrapped = docwrapper.wrap_masked_file(masked_text, file_name, fmt="docx")
        masked_file = {
            "base64": base64.b64encode(wrapped["bytes"]).decode("ascii"),
            "mimeType": wrapped["mime_type"],
            "fileName": wrapped["file_name"],
        }
    await emit({"type": "step", "step": 5, "label": "마스킹 완료", "done": True})

    return _build_result(
        original_text=text,
        masked_text=masked_text,
        pii_items=pii_items,
        injection_items=injection_items,
        scan_status=scan_status,
        reason=reason,
        blocked=blocked,
        masked_file=masked_file,
    )


def _build_result(
    *,
    original_text: str,
    masked_text: str,
    pii_items: list[Detection],
    injection_items: list[Detection],
    scan_status: str,
    reason: str | None,
    blocked: bool,
    masked_file: dict | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        # originalText 는 세션 중 반환용(HITL diff). store 에는 저장 금지(PLAN §9.1).
        "originalText": original_text,
        "maskedText": masked_text,
        "piiItems": pii_items,
        "injectionItems": injection_items,
        "truncated": False,
        "scanStatus": scan_status,
        "reason": reason,
        "blocked": blocked,
        "policy": {"injection": config.INJECTION_POLICY},
        "stats": {
            "piiCount": len(pii_items),
            "injectionCount": len(injection_items),
            "originalLength": len(original_text),
        },
    }
    if masked_file is not None:
        result["maskedFile"] = masked_file
    return result
