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
from app.core.parser import STATUS_OK, STATUS_TIMEOUT, parse_document

Emit = Callable[[dict], Awaitable[None]]

# 모델(pii encoder/injection llm_mcp)이 로컬에 준비되지 않았을 때의 scan_status.
# 파싱 관련 상태(STATUS_UNSUPPORTED 등)와 별개 축이라 parser 모듈이 아니라 여기 둔다.
MODELS_NOT_READY = "models_not_ready"


async def _noop_emit(_event: dict) -> None:
    return None


async def _parse_off_loop(file_bytes: bytes, mime_type: str, file_name: str) -> tuple[str, str, str | None]:
    """파싱을 이벤트 루프 밖에서 돌리고 상한을 건다.

    parse_document 는 동기 함수인데 예전엔 async 안에서 그대로 불렀다. pdfplumber 가
    무거운 PDF 를 붙들고 있는 동안 **이벤트 루프 전체가 멎어** /health·SSE·처리현황이
    같이 멈췄다 — 데스크탑 앱이 엔진이 죽은 걸로 오인할 수 있다.

    ponytail: wait_for 는 스레드를 죽이지 못한다 — 상한을 넘겨도 파싱 스레드는 뒤에서
    계속 돈다(요청만 제때 끝난다). 진짜로 끊으려면 파싱을 별도 프로세스로 빼야 하고
    그건 이 변경의 범위가 아니다. 지금 막는 것은 "한 파일이 엔진 전체를 붙잡는 것".
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(parse_document, file_bytes, mime_type, file_name),
            timeout=config.PARSE_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        return "", STATUS_TIMEOUT, f"파싱이 {config.PARSE_TIMEOUT_SEC:g}초를 넘겨 중단했습니다"


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


def _pii_spans_for_chunk(pii_items: list[Detection] | None, ch: dict) -> list[dict]:
    """원문 기준 PII 좌표를 이 청크 기준으로 옮긴다. 경계를 걸친 항목은 잘라서 넘긴다.

    예전엔 걸친 항목을 통째로 **제외**했다. 근거는 "청크는 100자씩 겹치므로 걸친
    항목도 이웃 청크에서는 온전히 들어온다" 였는데, 그 보장은 항목이 겹침보다 짧을
    때만 성립한다. chunk_size=1000 / step=900 에서 어느 청크에도 온전히 담기지
    못하는 최소 길이는 **102자**다(항목이 청크 시작에서 최대 899자 뒤에 놓일 수
    있으므로 1000-899=101자까지만 보장된다).

    그보다 긴 항목은 모든 청크에서 빠지고, 그러면 meta["pii_spans"] 가 비어
    build_redaction 이 "가릴 게 없다" 고 판단해 **그 자리를 원문 그대로 외부(Solar)로**
    보낸다. 주소가 공백으로 이어지면 계속 병합되는 경로가 실제로 있어
    (models/pii_engine/runtime/local_pii_inference.merge_lc_address) 가능성이 0 이 아니다.

    잘라서 넘기면 이 청크에 실제로 들어있는 부분은 전부 가려지고, 밖에 남은 부분은
    그 부분을 담은 청크가 자기 몫으로 가린다. 조각만 가려도 새는 것보다 낫다.
    """
    if not pii_items:
        return []
    lo = ch["offset"]
    hi = lo + len(ch["text"])
    out: list[dict] = []
    for it in pii_items:
        s = max(int(it["start"]), lo)
        e = min(int(it["end"]), hi)
        if s >= e:
            continue  # 이 청크와 겹치지 않는다
        out.append({"start": s - lo, "end": e - lo, "type": it.get("type", "OTHER_PII"),
                    "confidence": it.get("confidence", 1.0)})
    return out


async def _detect_all(
    detector,
    text: str,
    chunks: list[dict],
    *,
    user_prompt: str | None = None,
    user_prompt_masked: str | None = None,
    pii_items: list[Detection] | None = None,
) -> list[Detection]:
    """청크별 detect() 를 동시에 실행한다. 각 detector 는 GPU 추론 구간을 자체
    _request_lock 으로 이미 직렬화하므로(서브프로세스 하나 공유) 동시 호출해도
    그 구간은 그대로 순서대로 처리되지만, 인젝션 detector 의 Solar API 호출처럼
    락 밖에서 일어나는 네트워크 대기는 청크끼리 겹쳐서 진행된다 — 청크 2개가
    각각 Solar 를 부르는 문서에서 총 대기시간이 (콜1+콜2) 대신 max(콜1,콜2) 에
    가까워진다(실측).

    다만 그 겹침에는 상한이 있어야 한다. 예전엔 gather 로 청크를 전부 띄웠는데,
    위 이득은 청크가 몇 개인 문서를 전제로 한 것이라 문서가 길면 팬아웃이 그대로
    커졌다 — 실측: 50장(10만 자)이면 청크 111개가 한꺼번에 진입하고, 15만 자면
    167개다. 그만큼의 Solar 호출이 동시에 나가고 태스크도 그만큼 쌓인다.
    config.DETECT_CONCURRENCY 로 상한을 두되 기본값(8)이 웬만한 짧은 문서의 청크
    수보다 커서 작은 문서의 지연 이득은 그대로 남는다."""
    total = len(chunks)
    sem = asyncio.Semaphore(config.DETECT_CONCURRENCY)

    async def _run(idx: int, ch: dict) -> tuple[dict, list[Detection]]:
        meta: dict[str, Any] = {"chunk_index": idx, "total_chunks": total, "offset": ch["offset"]}
        if user_prompt:
            meta["user_prompt"] = user_prompt
        # 외부 API(Solar)로 나갈 때 가려야 할 것들. 로컬 모델은 원문을 그대로 본다.
        if pii_items:
            meta["pii_spans"] = _pii_spans_for_chunk(pii_items, ch)
        if user_prompt_masked is not None:
            meta["user_prompt_masked"] = user_prompt_masked
        async with sem:
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
        text, scan_status, reason = await _parse_off_loop(file_bytes or b"", mime_type, file_name)
    await emit({"type": "step", "step": 1, "label": "파싱 완료", "done": True})

    # 추출된 텍스트의 상한. 업로드는 바이트로 막지만 "풀린 길이" 는 아무도 안 봤다 —
    # 청크 수가 그대로 따라 늘고 청크마다 추론이 붙는다(config.MAX_TEXT_CHARS 주석 참고).
    # 조용히 자르지 않는다: 잘랐다는 사실을 결과(truncated)와 경고로 함께 내보낸다.
    truncated = False
    if scan_status == STATUS_OK and text and len(text) > config.MAX_TEXT_CHARS:
        full_len = len(text)
        text = text[: config.MAX_TEXT_CHARS]
        truncated = True
        await emit({
            "type": "warning",
            "partial": True,
            "reason": f"문서가 길어 앞 {config.MAX_TEXT_CHARS:,}자만 검사했습니다 (전체 {full_len:,}자)",
        })

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
            truncated=truncated,
        )

    # ── Step 2~3: 청크 + PII 탐지 ─────────────────────────────────────────────
    chunks = _split_chunks(text, config.CHUNK_SIZE)
    await emit({"type": "step", "step": 2, "label": f"PII 탐지 중 (총 {len(chunks)}개 청크)..."})
    pii_items = await _detect_all(registry.get_pii_detector(), text, chunks)
    await emit({"type": "step", "step": 2, "label": f"PII 탐지 완료 ({len(pii_items)}개)", "done": True})

    # ── user_prompt 자체도 PII 스캔(문서와 함께 보류됐다가 같이 넘어온 경우) ───
    # 인젝션 탐지보다 "먼저" 한다 — 인젝션 2차 위치특정이 외부 API(Solar)를 부를 때
    # 사용자 프롬프트도 함께 보내므로, 그 전에 가릴 것을 알고 있어야 한다.
    user_prompt_masked: str | None = None
    user_prompt_pii_items: list[Detection] = []
    if user_prompt:
        prompt_chunks = _split_chunks(user_prompt, config.CHUNK_SIZE)
        user_prompt_pii_items = await _detect_all(registry.get_pii_detector(), user_prompt, prompt_chunks)
        user_prompt_masked = masker.apply_masking(user_prompt, list(user_prompt_pii_items))["masked_text"]

    # ── Step 4: 인젝션 탐지 ───────────────────────────────────────────────────
    # pii_items/user_prompt_masked 를 함께 넘긴다 — 로컬 모델(EXAONE)은 원문을 보되,
    # 외부 Solar 로 나갈 때만 PII 를 가리기 위한 재료다(llm_mcp.detect 참고).
    await emit({"type": "step", "step": 4, "label": "인젝션 탐지 중..."})
    injection_items = await _detect_all(
        registry.get_injection_detector(),
        text,
        chunks,
        user_prompt=user_prompt,
        user_prompt_masked=user_prompt_masked,
        pii_items=pii_items,
    )
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
        truncated=truncated,
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
    truncated: bool = False,
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
        # 상한(config.MAX_TEXT_CHARS)을 넘겨 앞부분만 검사했는가. 소비자는 이 값으로
        # "전부 검사했다" 와 "일부만 검사했다" 를 구분한다.
        "truncated": truncated,
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
