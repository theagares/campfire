"""
app/core/pipeline/orchestrator.py
파이프라인 오케스트레이션 (PLAN §2, §6):
    parse → chunk(1500자) → detect(pii, injection) → mask → wrap

- PII/인젝션 위치는 모두 "원문 기준" 좌표로 정규화해 반환한다(익스텐션 계약).
- 파싱 실패/미지원/타임아웃은 예외로 죽지 않고 scan_status 로 통과 처리(PLAN §9.2).
- 인젝션 정책 mask(기본): [인젝션 마스킹] 치환 후 통과 / block: blocked=True.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Awaitable, Callable

from app import config
from app.core import model_status
from app.core.detectors import registry
from app.core.detectors.base import Detection
from app.core.masker import docwrapper, masker
from app.core.parser import STATUS_OK, parse_document

Emit = Callable[[dict], Awaitable[None]]

# 모델(pii encoder/injection llm_mcp)이 로컬에 준비되지 않았을 때의 scan_status.
# 파싱 관련 상태(STATUS_UNSUPPORTED 등)와 별개 축이라 parser 모듈이 아니라 여기 둔다.
MODELS_NOT_READY = "models_not_ready"


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


async def _detect_all(
    detector, text: str, chunks: list[dict], *, user_prompt: str | None = None
) -> list[Detection]:
    """청크별 detect() 를 동시에 실행한다. 각 detector 는 GPU 추론 구간을 자체
    _request_lock 으로 이미 직렬화하므로(서브프로세스 하나 공유) 동시 호출해도
    그 구간은 그대로 순서대로 처리되지만, 인젝션 detector 의 Solar API 호출처럼
    락 밖에서 일어나는 네트워크 대기는 청크끼리 겹쳐서 진행된다 — 청크 2개가
    각각 Solar 를 부르는 문서에서 총 대기시간이 (콜1+콜2) 대신 max(콜1,콜2) 에
    가까워진다(실측)."""
    total = len(chunks)

    async def _run(idx: int, ch: dict) -> tuple[dict, list[Detection]]:
        meta: dict[str, Any] = {"chunk_index": idx, "total_chunks": total, "offset": ch["offset"]}
        if user_prompt:
            meta["user_prompt"] = user_prompt
        dets = await detector.detect(ch["text"], meta=meta)
        return ch, dets

    pairs = await asyncio.gather(*(_run(idx, ch) for idx, ch in enumerate(chunks)))

    results: list[Detection] = []
    for ch, dets in pairs:
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
    user_prompt: str | None = None,
) -> dict[str, Any]:
    """텍스트(prompt) 또는 파일 파이프라인 공통 실행기.

    text 가 주어지면 프롬프트 경로, file_bytes 가 주어지면 파일 경로.

    user_prompt: 문서(파일/텍스트)와 "함께" 사용자가 실제로 보내려는 지시문.
    확장 프로그램이 문서 첨부를 곧바로 스캔하지 않고 사용자가 프롬프트를 보낼
    때까지 보류했다가 함께 넘기는 시나리오(§인젝션 탐지 재설계), 또는 MCP 가
    파일을 읽기 전에 이미 알고 있는 사용자 요청을 넘기는 시나리오에서 쓰인다.
    주어지면 인젝션 탐지가 이 문자열을 실제 user_prompt 로 사용해(§base.ChunkMeta
    .user_prompt) placeholder 대신 실제 정렬 판단 근거로 삼는다. 프롬프트 자체도
    PII 스캔해 결과에 함께 포함한다(아래 _build_result 의 userPrompt* 필드).
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

    # 룰베이스 폴백을 없앴다 — pii/injection 모두 실 모델(encoder/llm_mcp)만 남아서,
    # 가중치가 아직 안 받아진 상태로 detect() 를 부르면 서브프로세스가 로딩에 실패해
    # 예외로 죽는다. 파싱은 이미 끝났으니(원문은 그대로 확보) 탐지만 생략하고 위와
    # 같은 미검사 통과 경로로 넘긴다 — "모델이 없으면 조용히 룰베이스로 격하"가 아니라
    # "모델이 없으면 아예 검사하지 않는다"는 게 이번 변경의 핵심이다.
    if not model_status.all_ready():
        await emit({
            "type": "warning",
            "scanStatus": MODELS_NOT_READY,
            "reason": "PII/인젝션 모델이 아직 준비되지 않았습니다",
        })
        return _build_result(
            original_text=text,
            masked_text=text,
            pii_items=[],
            injection_items=[],
            scan_status=MODELS_NOT_READY,
            reason="PII/인젝션 모델이 아직 준비되지 않았습니다 — 다운로드가 끝나면 다시 시도하세요.",
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
    injection_items = await _detect_all(
        registry.get_injection_detector(), text, chunks, user_prompt=user_prompt
    )
    await emit({"type": "step", "step": 4, "label": f"인젝션 탐지 완료 ({len(injection_items)}개)", "done": True})

    # ── user_prompt 자체도 PII 스캔(문서와 함께 보류됐다가 같이 넘어온 경우) ───
    user_prompt_masked: str | None = None
    user_prompt_pii_items: list[Detection] = []
    if user_prompt:
        prompt_chunks = _split_chunks(user_prompt, config.CHUNK_SIZE)
        user_prompt_pii_items = await _detect_all(registry.get_pii_detector(), user_prompt, prompt_chunks)
        user_prompt_masked = masker.apply_masking(user_prompt, list(user_prompt_pii_items))["masked_text"]

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
        user_prompt=user_prompt,
        user_prompt_masked=user_prompt_masked,
        user_prompt_pii_items=user_prompt_pii_items,
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
    user_prompt: str | None = None,
    user_prompt_masked: str | None = None,
    user_prompt_pii_items: list[Detection] | None = None,
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
    if user_prompt is not None:
        # 문서와 "함께" 넘어온 실제 사용자 프롬프트의 PII 스캔 결과 — 확장 프로그램이
        # 문서 첨부를 보류했다가 프롬프트 전송 시점에 함께 넘긴 경우에만 채워진다.
        result["userPromptOriginal"] = user_prompt
        result["userPromptMasked"] = user_prompt_masked
        result["userPromptPiiItems"] = user_prompt_pii_items or []
    return result
