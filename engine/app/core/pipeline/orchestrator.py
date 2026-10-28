"""
app/core/pipeline/orchestrator.py
파이프라인 오케스트레이션 (PLAN §2, §6):
    parse → PII 문자 청크 / injection 토큰 청크 → detect → mask → wrap

- PII/인젝션 위치는 모두 "원문 기준" 좌표로 정규화해 반환한다(익스텐션 계약).
- 파싱 실패/미지원/타임아웃은 예외로 죽지 않고 scan_status 로 통과 처리(PLAN §9.2).
- 인젝션 정책 mask(기본): [인젝션 마스킹] 치환 후 통과 / block: blocked=True.
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any, Awaitable, Callable

from app import config
from app.core import forced_mask, model_status
from app.core.detectors import combined_gpu, confidential, image_ocr, registry
from app.core.detectors.pii import credentials
from app.core.detectors.base import Detection
from app.core.masker import docwrapper, masker
from app.core.parser import STATUS_FAILED, STATUS_OK, STATUS_TIMEOUT
from app.core.parser import images as image_marks
from app.core.parser.process_pool import (
    ParserPoolBusy,
    ParserTimedOut,
    ParserWorkerError,
    get_default_parse_pool,
)
from app.core.pipeline import injection_chunker

Emit = Callable[[dict], Awaitable[None]]

# 모델(pii encoder/injection llm_mcp)이 로컬에 준비되지 않았을 때의 scan_status.
# 파싱 관련 상태(STATUS_UNSUPPORTED 등)와 별개 축이라 parser 모듈이 아니라 여기 둔다.
MODELS_NOT_READY = "models_not_ready"


async def _noop_emit(_event: dict) -> None:
    return None


async def _parse_off_loop(
    file_bytes: bytes, mime_type: str, file_name: str, images: list[bytes] | None = None,
) -> tuple[str, str, str | None]:
    """제한된 별도 프로세스에서 파싱하고 타임아웃 워커를 실제 종료한다."""
    try:
        result, extracted_images = await get_default_parse_pool().parse(
            file_bytes,
            mime_type,
            file_name,
            collect_images=images is not None,
            timeout=config.PARSE_TIMEOUT_SEC,
        )
        if images is not None and extracted_images:
            images.extend(extracted_images)
        return result
    except ParserTimedOut:
        return "", STATUS_TIMEOUT, f"파싱이 {config.PARSE_TIMEOUT_SEC:g}초를 넘겨 중단했습니다"
    except ParserPoolBusy:
        return "", STATUS_TIMEOUT, "파싱 대기열이 가득 차 검사를 시작하지 못했습니다"
    except ParserWorkerError as exc:
        return "", STATUS_FAILED, f"파서 프로세스 오류: {exc}"


def _split_chunks(text: str, chunk_size: int, overlap: int = 100) -> list[dict]:
    """문자 청크 분할(+겹침). 경계에서 매치가 잘리는 것을 겹침으로 완화."""
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


def _prefer_injection_for_nested_pii(
    pii_items: list[Detection],
    injection_items: list[Detection],
) -> list[Detection]:
    """Drop PII spans fully covered by an injection span.

    The injection item already masks the entire covered range.  Keeping a nested
    PII item would make the same text appear in both result categories and could
    let the masker/UI select the PII label instead.  Partial overlaps stay in the
    PII result because the injection does not cover the complete sensitive span.
    """
    if not pii_items or not injection_items:
        return pii_items

    injection_spans = [
        (item["start"], item["end"])
        for item in injection_items
    ]
    return [
        item
        for item in pii_items
        if not any(
            injection_start <= item["start"]
            and item["end"] <= injection_end
            for injection_start, injection_end in injection_spans
        )
    ]


async def _detect_pii(text: str) -> list[Detection]:
    """PII 탐지 = 모델 탐지 + 자격증명 정규식.

    자격증명(API 키/토큰/개인키/비밀번호)은 개인정보가 아니라서 PII 모델의 라벨
    체계에 없다. 그런데 이 게이트웨이를 통과한 텍스트는 그대로 외부 AI 서비스로
    나간다 — 실측(2026-09-06)에서 AWS 키·GitHub 토큰·RSA 개인키가 piiItems 0건으로
    통과했다.

    _detect_all 안이 아니라 여기에 두는 이유: _detect_all 은 인젝션 탐지도 함께
    쓰는 공용 경로라, 거기 넣으면 인젝션 패스에서도 같은 자격증명이 한 번 더 잡힌다.

    자격증명 탐지는 청크를 타지 않고 원문 전체를 한 번에 본다. PEM 블록처럼 여러
    줄에 걸친 항목이 청크 경계에서 잘리면 안 되기 때문이다.
    """
    # EncoderPiiDetector가 원문을 400/60 윈도우로 나눠 한 번의 sidecar 요청 안에서
    # 배치 추론한다. 여기서 먼저 1000/100으로 자르면 겹친 부분을 다시 윈도잉해
    # 중복 forward와 경계 조각 오탐이 늘어나므로 원문 전체를 한 청크로 넘긴다.
    whole_text = [{"text": text, "offset": 0}]
    model_items = await _detect_all(registry.get_pii_detector(), text, whole_text)
    return _dedupe(list(model_items) + credentials.detect(text))


async def _detect_pii_with_keep(
    text: str,
) -> tuple[list[Detection], list[dict[str, Any]]]:
    """PII 결과와 후단 MLP가 명시적으로 KEEP한 후보를 함께 얻는다.

    KEEP은 시연 대시보드 표시용일 뿐 Detection에 섞지 않는다. 진단 기능을 모르는
    대체 detector에서는 정상 탐지만 수행하고 빈 KEEP 목록으로 폴백한다.
    """
    detector = registry.get_pii_detector()
    detect_with_keep = getattr(detector, "detect_with_keep", None)
    if not callable(detect_with_keep):
        return await _detect_pii(text), []
    model_items, kept = await detect_with_keep(text, meta=None)
    return _dedupe(list(model_items) + credentials.detect(text)), kept


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
    direct_prompt: bool = False,
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

    def _meta(idx: int, ch: dict) -> dict[str, Any]:
        meta: dict[str, Any] = {"chunk_index": idx, "total_chunks": total, "offset": ch["offset"]}
        if user_prompt:
            meta["user_prompt"] = user_prompt
        if direct_prompt:
            meta["direct_prompt"] = True
        # 외부 API(Solar)로 나갈 때 가려야 할 것들. 로컬 모델은 원문을 그대로 본다.
        if pii_items:
            meta["pii_spans"] = _pii_spans_for_chunk(pii_items, ch)
        if user_prompt_masked is not None:
            meta["user_prompt_masked"] = user_prompt_masked
        return meta

    async def _run(idx: int, ch: dict) -> tuple[dict, list[Detection]]:
        async with sem:
            dets = await detector.detect(ch["text"], meta=_meta(idx, ch))
        return ch, dets

    batch_detect = getattr(detector, "detect_batch", None)
    if callable(batch_detect):
        indexed = list(enumerate(chunks))

        async def _run_batch(group: list[tuple[int, dict]]) -> list[tuple[dict, list[Detection]]]:
            async with sem:
                detections = await batch_detect(
                    [chunk["text"] for _, chunk in group],
                    metas=[_meta(index, chunk) for index, chunk in group],
                )
            if len(detections) != len(group):
                raise RuntimeError("detector batch response count does not match request")
            return [
                (chunk, rows)
                for (_, chunk), rows in zip(group, detections, strict=True)
            ]

        groups = [
            indexed[start : start + config.INJECTION_BATCH_SIZE]
            for start in range(0, len(indexed), config.INJECTION_BATCH_SIZE)
        ]
        nested = await asyncio.gather(*(_run_batch(group) for group in groups))
        pairs = [pair for group in nested for pair in group]
    else:
        pairs = await asyncio.gather(*(_run(idx, ch) for idx, ch in enumerate(chunks)))

    results: list[Detection] = []
    for ch, dets in pairs:
        for d in dets:
            d["start"] += ch["offset"]
            d["end"] += ch["offset"]
            d["text"] = text[d["start"] : d["end"]]
            results.append(d)
    return _dedupe(results)


def _supports_staged_injection(detector: Any) -> bool:
    """Whether the detector can separate local classification from Solar."""
    return callable(getattr(detector, "classify_batch_local", None)) and callable(
        getattr(detector, "finalize_batch_local", None)
    )


async def _classify_injection_locally(
    detector: Any,
    chunks: list[dict],
    *,
    user_prompt: str | None = None,
) -> list[tuple[dict, dict[str, Any]]]:
    """Run only EXAONE while PII detection is in flight.

    No external API is called here. The returned local decisions are deliberately
    kept separate from detections until PII spans are available for Solar redaction.
    """
    classify = detector.classify_batch_local
    indexed = list(enumerate(chunks))
    groups = [
        indexed[start : start + config.INJECTION_BATCH_SIZE]
        for start in range(0, len(indexed), config.INJECTION_BATCH_SIZE)
    ]
    sem = asyncio.Semaphore(config.DETECT_CONCURRENCY)

    async def _run_batch(
        group: list[tuple[int, dict]],
    ) -> list[tuple[dict, dict[str, Any]]]:
        async with sem:
            results = await classify(
                [chunk["text"] for _, chunk in group],
                user_prompts=[user_prompt for _ in group],
            )
        if len(results) != len(group):
            raise RuntimeError("local injection response count does not match request")
        return [
            (chunk, result)
            for (_, chunk), result in zip(group, results, strict=True)
        ]

    nested = await asyncio.gather(*(_run_batch(group) for group in groups))
    return [pair for group in nested for pair in group]


async def _try_combined_local_inference(
    pii_detector: Any,
    injection_detector: Any,
    text: str,
    injection_chunks: list[dict],
    *,
    user_prompt: str | None = None,
) -> tuple[
    list[Detection],
    list[Detection],
    list[tuple[dict, dict[str, Any]]],
] | tuple[
    list[Detection],
    list[Detection],
    list[tuple[dict, dict[str, Any]]],
    list[dict[str, Any]],
] | None:
    """PII와 EXAONE을 한 CUDA 프로세스의 두 stream에서 실행한다.

    worker는 로컬 원시 추론만 수행한다. Solar 호출은 이 함수가 끝나 PII 좌표가
    준비된 뒤 기존 ``_finalize_staged_injection``에서만 일어난다. 통합 worker를
    사용할 수 없으면 ``None``을 반환해 기존 두 sidecar 병렬 경로로 폴백한다.
    """
    prepare = getattr(pii_detector, "prepare_windows", None)
    decode = getattr(pii_detector, "decode_windows", None)
    if (
        not config.COMBINED_GPU_WORKER_ENABLED
        or not _supports_staged_injection(injection_detector)
        or not callable(prepare)
        or not callable(decode)
    ):
        return None

    document_windows = prepare(text)
    prompt_windows = prepare(user_prompt) if user_prompt else []
    pii_texts = [window for window, _ in document_windows]
    pii_texts.extend(window for window, _ in prompt_windows)
    if not pii_texts:
        return None

    # 빈·공백 청크는 모델에 보내지 않는다(개별 경로의 classify_batch_local 과 같다). 빈 문자열은
    # 토큰이 0개라 워커 요청 전체가 실패했다 — 스캔본 PDF(추출 텍스트 "") + 사용자 프롬프트.
    valid_indices = [i for i, chunk in enumerate(injection_chunks) if chunk["text"].strip()]
    injection_records = [
        {
            "system_prompt": config.INJECTION_LLM_SYSTEM_PROMPT,
            "user_prompt": user_prompt or config.INJECTION_LLM_USER_PROMPT,
            "tool_response": injection_chunks[i]["text"],
        }
        for i in valid_indices
    ]
    worker = combined_gpu.get_worker()
    try:
        if config.DEMO_TRACE_ENABLED:
            infer_with_keep = getattr(worker, "infer_parallel_with_pii_keep", None)
            if not callable(infer_with_keep):
                raise combined_gpu.CombinedGpuUnavailable(
                    "combined worker does not expose PII KEEP decisions"
                )
            entity_lists, keep_lists, valid_results = await infer_with_keep(
                pii_texts, injection_records
            )
        else:
            entity_lists, valid_results = await worker.infer_parallel(
                pii_texts, injection_records
            )
            keep_lists = [[] for _ in pii_texts]
        local_results: list[dict[str, Any]] = [
            {"is_injection": False, "scores": {}} for _ in injection_chunks
        ]
        for i, result in zip(valid_indices, valid_results, strict=True):
            local_results[i] = result
        document_count = len(document_windows)
        document_entities = entity_lists[:document_count]
        prompt_entities = entity_lists[document_count:]
        document_kept = keep_lists[:document_count]
        pii_items = _dedupe(
            list(decode(text, document_windows, document_entities))
            + credentials.detect(text)
        )
        user_pii_items = (
            _dedupe(
                list(decode(user_prompt, prompt_windows, prompt_entities))
                + credentials.detect(user_prompt)
            )
            if user_prompt
            else []
        )
        decode_keep = getattr(pii_detector, "decode_keep_windows", None)
        pii_keep = (
            decode_keep(text, document_windows, document_kept)
            if config.DEMO_TRACE_ENABLED and callable(decode_keep)
            else []
        )
        classified = list(zip(injection_chunks, local_results, strict=True))
    except combined_gpu.CombinedGpuUnavailable:
        return None
    except Exception:
        # 응답 형식/복원 단계까지 포함해 통합 경로에 문제가 생기면 같은 요청을
        # 검증된 개별 sidecar로 재실행한다. 연속 실패가 쌓이면 worker 가 스스로 꺼져
        # 매 요청을 지연시키지 않는다(한 번의 실패로 끄지는 않는다).
        await worker.fail()
        return None

    for detector in (pii_detector, injection_detector):
        residency = getattr(detector, "residency", None)
        if residency is not None:
            residency.device = "cuda"
            residency.touch()
    if config.DEMO_TRACE_ENABLED:
        return pii_items, user_pii_items, classified, pii_keep
    return pii_items, user_pii_items, classified


async def _finalize_staged_injection(
    detector: Any,
    text: str,
    classified: list[tuple[dict, dict[str, Any]]],
    *,
    user_prompt: str | None = None,
    user_prompt_masked: str | None = None,
    pii_items: list[Detection] | None = None,
    direct_prompt: bool = False,
) -> list[Detection]:
    """Run PII-safe Solar localization after both local models have joined."""
    total = len(classified)
    finalize = detector.finalize_batch_local
    indexed = list(enumerate(classified))
    # 청크 하나씩 세마포어 안에서 마무리한다. 묶음(8개) 단위로 잡으면 묶음 안에서 Solar 가
    # 8개씩 동시에 나가 문서당 동시 호출이 DETECT_CONCURRENCY×8(64)까지 늘었다.
    groups = [[item] for item in indexed]
    sem = asyncio.Semaphore(config.DETECT_CONCURRENCY)

    def _meta(index: int, chunk: dict) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "chunk_index": index,
            "total_chunks": total,
            "offset": chunk["offset"],
        }
        if user_prompt:
            meta["user_prompt"] = user_prompt
        if direct_prompt:
            meta["direct_prompt"] = True
        if pii_items:
            meta["pii_spans"] = _pii_spans_for_chunk(pii_items, chunk)
        if user_prompt_masked is not None:
            meta["user_prompt_masked"] = user_prompt_masked
        return meta

    async def _run_batch(
        group: list[tuple[int, tuple[dict, dict[str, Any]]]],
    ) -> list[tuple[dict, list[Detection]]]:
        chunks = [chunk for _, (chunk, _) in group]
        local_results = [result for _, (_, result) in group]
        async with sem:
            detections = await finalize(
                [chunk["text"] for chunk in chunks],
                local_results,
                metas=[_meta(index, chunk) for (index, _), chunk in zip(group, chunks, strict=True)],
            )
        if len(detections) != len(group):
            raise RuntimeError("final injection response count does not match request")
        return list(zip(chunks, detections, strict=True))

    nested = await asyncio.gather(*(_run_batch(group) for group in groups))
    pairs = [pair for group in nested for pair in group]
    results: list[Detection] = []
    for chunk, detections in pairs:
        for detection in detections:
            detection["start"] += chunk["offset"]
            detection["end"] += chunk["offset"]
            detection["text"] = text[detection["start"] : detection["end"]]
            results.append(detection)
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
    direct_prompt: bool = False,
    scan_injection: bool = True,
) -> dict[str, Any]:
    """텍스트(prompt) 또는 파일 파이프라인 공통 실행기.

    text 가 주어지면 프롬프트 경로, file_bytes 가 주어지면 파일 경로.

    direct_prompt: text 가 문서가 아니라 사용자가 AI 사이트에 직접 쓴 프롬프트다
    (/jobs/prompt). 인젝션 2차(Solar)가 "문서 속 지시" 기준 대신 사용자 본인의 작업
    요청은 통과시키는 기준으로 본다(llm_mcp._SOLAR_DIRECT_PROMPT_SYSTEM_PROMPT).

    user_prompt: 문서(파일/텍스트)와 "함께" 사용자가 실제로 보내려는 지시문.
    확장 프로그램이 문서 첨부를 곧바로 스캔하지 않고 사용자가 프롬프트를 보낼
    때까지 보류했다가 함께 넘기는 시나리오(§인젝션 탐지 재설계), 또는 MCP 가
    파일을 읽기 전에 이미 알고 있는 사용자 요청을 넘기는 시나리오에서 쓰인다.
    주어지면 인젝션 탐지가 이 문자열을 실제 user_prompt 로 사용해(§base.ChunkMeta
    .user_prompt) placeholder 대신 실제 정렬 판단 근거로 삼는다. 프롬프트 자체도
    PII 스캔해 결과에 함께 포함한다(아래 _build_result 의 userPrompt* 필드).

    scan_injection: False 이면 입력 본문에 대한 인젝션 탐지를 전부 생략한다. 확장
    프로그램의 사용자 프롬프트 전용 경로가 사용하며 PII·자격증명·강제 마스킹은
    그대로 수행한다. 파일/MCP 등 일반 텍스트 검사는 기본값 True 를 유지한다.
    """
    emit = emit or _noop_emit
    scan_status = STATUS_OK
    reason: str | None = None
    forced_snapshot = forced_mask.registry.snapshot()

    # ── Step 1: 파싱 ──────────────────────────────────────────────────────────
    await emit({"type": "step", "step": 1, "label": "입력 파싱 중..."})
    # 이미지 속 글자 검사(선택 기능): 파서가 이미지 파일·DOCX·PPTX 속 그림을 담고 본문에 표시를 남기면, 여기서 VARCO 로
    # 읽어 그 자리에 넣는다(파싱 스레드 안에서는 사이드카를 부를 수 없다). 꺼져 있으면 images=None — 파서 결과가 그대로다.
    images: list[bytes] | None = None
    image_infos: list[dict[str, Any]] = []
    images_removed = 0
    if text is None:
        # 꺼져 있어도 문서 속 그림은 센다 — 검사 못 한 그림째 원본을 보내지 않고 글만 보낸다(사용자 결정 2026-10-28,
        # 확장·프록시가 imagesRemoved 를 보고 원본 대신 글 .md 를 보낸다). 이미지 파일 자체는 꺼져 있으면 지금처럼 미지원이다
        # (목록을 주면 파서가 그림 한 장짜리 문서로 받는다).
        collect = config.IMAGE_OCR_ENABLED or not image_marks.is_image(
            os.path.splitext(file_name or "")[1].lower(), mime_type or "")
        images = [] if collect else None
        text, scan_status, reason = await _parse_off_loop(file_bytes or b"", mime_type, file_name, images)
        if images and scan_status == STATUS_OK:
            if config.IMAGE_OCR_ENABLED:
                text, image_infos = await image_ocr.fill(text, images, emit)
            else:
                text, images_removed = image_marks.strip_marks(text), len(images)
    images_unchecked = any(i["status"] == "unchecked" for i in image_infos)
    images_read = [i for i in image_infos if i["status"] == "read"]
    await emit({"type": "step", "step": 1, "label": "파싱 완료", "done": True})

    # 추출된 텍스트의 상한. 업로드는 바이트로 막지만 "풀린 길이" 는 아무도 안 봤다 —
    # 청크 수가 그대로 따라 늘고 청크마다 추론이 붙는다(config.MAX_TEXT_CHARS 주석 참고).
    # 조용히 자르지 않는다: 잘랐다는 사실을 결과(truncated)와 경고로 함께 내보낸다.
    truncated = False
    original_chars = len(text) if text else 0
    if scan_status == STATUS_OK and text and len(text) > config.MAX_TEXT_CHARS:
        text = text[: config.MAX_TEXT_CHARS]
        truncated = True
        await emit({
            "type": "warning",
            "partial": True,
            "reason": f"문서가 길어 앞 {config.MAX_TEXT_CHARS:,}자만 검사했습니다 (전체 {original_chars:,}자)",
        })

    # Mandatory literal rules do not depend on either ML model.  Capture one
    # immutable snapshot per request so a settings update cannot mix old and
    # new rules inside the same document.
    forced_items = forced_snapshot.find(text or "") if forced_snapshot.ready else []
    user_prompt_forced_items = (
        forced_snapshot.find(user_prompt or "") if forced_snapshot.ready and user_prompt else []
    )
    forced_complete = scan_status == STATUS_OK and not truncated
    # 파일명에도 등록어가 있을 수 있다 — 내용을 가려도 "Aurora_계획_masked.md" 로 샌다.
    # 단어는 내보내지 않고 사실만 알린다. 소비자는 원본 전송을 막고 일반 이름을 쓴다.
    name_forced = bool(
        forced_snapshot.ready and file_bytes is not None and forced_snapshot.find(file_name or "")
    )
    forced_policy = {
        "fileNameForced": name_forced,
        "managed": forced_snapshot.managed,
        "ready": forced_snapshot.ready,
        "active": forced_snapshot.active,
        "count": len(forced_snapshot.terms),
        "revision": forced_snapshot.revision,
        "complete": forced_complete,
    }

    # A desktop-managed engine must not scan with an empty rule set before the
    # app has delivered the encrypted configuration.  That transient state is
    # fail-closed rather than silently treating it as "no rules".
    if not forced_snapshot.ready:
        # 규칙은 데스크탑 앱이 보내 준다. 맥에서는 새 버전·판 교체 뒤 첫 실행 때 앱이 키체인
        # 승인 창 응답을 기다리며 멈춰 있는 경우가 대표적이라(맥 실측), 확장 화면에서 원인을
        # 짐작할 수 있게 함께 알린다.
        policy_reason = (
            "강제 마스킹 규칙을 아직 불러오지 못했습니다 — Campfire 앱이 실행 중인지, "
            "macOS 라면 키체인 승인 창('Campfire Safe Storage')이 떠 있지 않은지 확인하세요"
        )
        await emit({"type": "warning", "scanStatus": "policy_not_ready", "reason": policy_reason})
        return _build_result(
            original_text=text or "",
            masked_text=text or "",
            pii_items=[],
            injection_items=[],
            forced_mask_items=[],
            scan_status="policy_not_ready",
            reason=policy_reason,
            blocked=True,
            masked_file=None,
            truncated=truncated,
            original_chars=original_chars,
            forced_policy=forced_policy,
        )

    # 파싱 실패/미지원은 기존에는 미검사 통과였다. 강제 규칙이 하나라도 켜져 있으면
    # 내용을 확인할 수 없는 원본을 내보내는 순간 "항상 마스킹" 보장이 깨지므로 차단한다.
    if scan_status != STATUS_OK:
        await emit({"type": "warning", "scanStatus": scan_status, "reason": reason})
        return _build_result(
            original_text=text or "",
            masked_text=text or "",
            pii_items=[],
            injection_items=[],
            forced_mask_items=forced_items,
            scan_status=scan_status,
            reason=reason,
            blocked=forced_snapshot.active,
            masked_file=None,
            original_chars=original_chars,
            forced_policy=forced_policy,
        )

    # 룰베이스 폴백을 없앴다 — pii/injection 모두 실 모델(encoder/llm_mcp)만 남아서,
    # 가중치가 아직 안 받아진 상태로 detect() 를 부르면 서브프로세스가 로딩에 실패해
    # 예외로 죽는다. 파싱은 이미 끝났으니(원문은 그대로 확보) 탐지만 생략하고 위와
    # 같은 미검사 통과 경로로 넘긴다 — "모델이 없으면 조용히 룰베이스로 격하"가 아니라
    # "모델이 없으면 아예 검사하지 않는다"는 게 이번 변경의 핵심이다.
    required_models_ready = (
        model_status.all_ready() if scan_injection else model_status.pii_ready()
    )
    if not required_models_ready:
        missing_models = "PII/인젝션" if scan_injection else "PII"
        await emit({
            "type": "warning",
            "scanStatus": MODELS_NOT_READY,
            "reason": f"{missing_models} 모델이 아직 준비되지 않았습니다",
        })
        # 다만 자격증명은 여기서도 잡는다. credentials.detect 는 가중치가 필요 없는
        # 정규식이라(모듈 docstring) 모델 상태와 무관하게 돈다. 오히려 이 창(설치
        # 직후 다운로드 중)이 제일 위험하다 — 여기서 건너뛰면 secure_read_file(".env")
        # 이 원문 그대로 decision:"clean" 으로 나간다(#147 이 막으려던 그 누출).
        cred_items = credentials.detect(text)
        user_prompt_masked = None
        if user_prompt is not None:
            user_prompt_masked = masker.apply_masking(
                user_prompt, list(user_prompt_forced_items)
            )["masked_text"]
        return _build_result(
            original_text=text,
            masked_text=masker.apply_masking(
                text, list(cred_items) + list(forced_items)
            )["masked_text"],
            pii_items=cred_items,
            injection_items=[],
            forced_mask_items=forced_items,
            scan_status=MODELS_NOT_READY,
            reason=f"{missing_models} 모델이 아직 준비되지 않았습니다 — 다운로드가 끝나면 다시 시도하세요.",
            blocked=forced_snapshot.active and truncated,
            masked_file=None,
            truncated=truncated,
            original_chars=original_chars,
            user_prompt=user_prompt,
            user_prompt_masked=user_prompt_masked,
            user_prompt_pii_items=[],
            user_prompt_forced_mask_items=user_prompt_forced_items,
            forced_policy=forced_policy,
        )

    # ── Step 2~4: PII + 선택적 로컬 인젝션 병렬 탐지 ─────────────────────────
    # 사용자 프롬프트 전용 HTTP 경로는 scan_injection=False 로 들어온다. 이때는 PII만
    # 검사하며 injection detector/토크나이저/Solar를 전혀 건드리지 않는다. 파일·MCP
    # 경로는 기본값 True라 기존 문서 인젝션 검사를 그대로 수행한다.
    await emit({
        "type": "step",
        "step": 2,
        "label": (
            f"PII 탐지 중 ({config.PII_WINDOW_SIZE}자/"
            f"{config.PII_WINDOW_OVERLAP}자 겹침)..."
        ),
    })
    classified: list[tuple[dict, dict[str, Any]]] | None = None
    injection_detector: Any | None = None
    injection_chunks: list[dict] = []
    pii_keep: list[dict[str, Any]] = []
    if not scan_injection:
        if config.DEMO_TRACE_ENABLED:
            pii_items, pii_keep = await _detect_pii_with_keep(text)
        else:
            pii_items = await _detect_pii(text)
        user_prompt_pii_items = await _detect_pii(user_prompt) if user_prompt else []
        injection_items: list[Detection] = []
    else:
        # EXAONE의 로컬 분류는 원문을 보며 외부로 아무것도 보내지 않으므로 PII와
        # 동시에 실행한다. 둘 다 끝난 뒤에만 마스킹 좌표와 함께 Solar 위치 확인을 한다.
        await emit({"type": "step", "step": 4, "label": "인젝션 토큰 청킹 중..."})
        pii_detector = registry.get_pii_detector()
        injection_detector = registry.get_injection_detector()
        staged_injection = _supports_staged_injection(injection_detector)
        injection_chunks = await asyncio.to_thread(
            injection_chunker.split_injection_chunks, text
        )
        await emit({
            "type": "step",
            "step": 4,
            "label": (
                f"인젝션 로컬 탐지 중 (총 {len(injection_chunks)}개, "
                f"{config.INJECTION_CHUNK_TOKENS}토큰/"
                f"{config.INJECTION_CHUNK_OVERLAP_TOKENS}토큰 겹침)..."
            ),
        })

        combined_result = await _try_combined_local_inference(
            pii_detector,
            injection_detector,
            text,
            injection_chunks,
            user_prompt=user_prompt,
        )
        if combined_result is not None:
            if config.DEMO_TRACE_ENABLED:
                pii_items, user_prompt_pii_items, classified, pii_keep = combined_result
            else:
                pii_items, user_prompt_pii_items, classified = combined_result
        else:
            # CUDA가 없거나 통합 worker가 실패하면 검증된 개별 sidecar 경로를 그대로 쓴다.
            # 두 로컬 모델은 서로 다른 프로세스지만 기존처럼 동시에 실행한다.
            # 처음 띄울 때는 PII부터 올려 느린 CPU에서 두 모델의 동시 로딩으로 PII
            # 기동 시간 상한을 넘기지 않게 한다. 이미 떠 있으면 즉시 지나간다.
            ensure_pii = getattr(pii_detector, "_ensure_process", None)
            if callable(ensure_pii):
                await ensure_pii()
            classified_task: asyncio.Task[
                list[tuple[dict, dict[str, Any]]]
            ] | None = None
            user_pii_task: asyncio.Task[list[Detection]] | None = None
            pii_task = asyncio.create_task(
                _detect_pii_with_keep(text)
                if config.DEMO_TRACE_ENABLED
                else _detect_pii(text)
            )
            running_tasks: list[asyncio.Task[Any]] = [pii_task]
            if user_prompt:
                user_pii_task = asyncio.create_task(_detect_pii(user_prompt))
                running_tasks.append(user_pii_task)
            if staged_injection:
                classified_task = asyncio.create_task(
                    _classify_injection_locally(
                        injection_detector,
                        injection_chunks,
                        user_prompt=user_prompt,
                    )
                )
                running_tasks.append(classified_task)

            try:
                pii_result = await pii_task
                if config.DEMO_TRACE_ENABLED:
                    pii_items, pii_keep = pii_result
                else:
                    pii_items = pii_result
                if user_pii_task is not None:
                    user_prompt_pii_items = await user_pii_task
                else:
                    user_prompt_pii_items = []
                if classified_task is not None:
                    classified = await classified_task
            except BaseException:
                # 한 단계가 실패하거나 요청이 취소되면 다른 sidecar 요청을 백그라운드에
                # 남기지 않는다. gather와 달리 원래 예외 형식도 ExceptionGroup으로 바뀌지 않는다.
                for task in running_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*running_tasks, return_exceptions=True)
                raise

    await emit({
        "type": "step",
        "step": 2,
        "label": f"PII 탐지 완료 ({len(pii_items)}개)",
        "done": True,
    })

    # user_prompt도 Solar에 들어갈 수 있으므로 PII 결과가 준비된 뒤 마스킹한다.
    user_prompt_masked: str | None = None
    if user_prompt:
        user_prompt_masked = masker.apply_masking(
            user_prompt, list(user_prompt_pii_items) + list(user_prompt_forced_items)
        )["masked_text"]

    if scan_injection:
        assert injection_detector is not None
        if classified is not None:
            # 여기까지 오면 문서와 사용자 프롬프트의 PII 스캔이 모두 끝났다. 이제부터만
            # 외부 Solar를 호출할 수 있으며, 각 청크의 PII 좌표를 함께 전달한다.
            injection_items = await _finalize_staged_injection(
                injection_detector,
                text,
                classified,
                user_prompt=user_prompt,
                user_prompt_masked=user_prompt_masked,
                pii_items=list(pii_items) + list(forced_items),
                direct_prompt=direct_prompt,
            )
        else:
            # 테스트/대체 detector가 단계 분리를 지원하지 않으면 기존 순차 계약을 유지한다.
            injection_items = await _detect_all(
                injection_detector,
                text,
                injection_chunks,
                user_prompt=user_prompt,
                user_prompt_masked=user_prompt_masked,
                pii_items=list(pii_items) + list(forced_items),
                direct_prompt=direct_prompt,
            )
        # Solar가 꺼져 있으면 양성 윈도우 전체를 fail-safe로 반환하므로 인접 윈도우가
        # 겹친다. 문서 단위 OR 결과는 유지하되 API/통계에는 하나의 연속 구간으로 보인다.
        injection_items = masker.merge_overlapping(injection_items)
        for item in injection_items:
            item["text"] = text[item["start"] : item["end"]]
        pii_items = _prefer_injection_for_nested_pii(pii_items, injection_items)
        await emit({"type": "step", "step": 4, "label": f"인젝션 탐지 완료 ({len(injection_items)}개)", "done": True})

    # ── Step 3: 기밀 탐지 (회사 KG, 선택 기능) ────────────────────────────────
    # PII·인젝션 다음 — Solar 로 보내는 문서에서 PII·사용자 지정 단어·인젝션 구간을 가린다.
    # 결과는 piiItems 에 실린다(CONFIDENTIAL). 단계 번호 3 은 진행 문구용(사이드 패널 막대는 1·2·4·5).
    # 차단될 문서(인젝션 차단 정책, 사용자 지정 규칙이 켜진 채 잘린 문서)는 어차피 나가지 않으므로
    # Solar 로도 보내지 않는다 — 그래서 차단 여부를 먼저 정한다.
    # ── 정책: block 이면 인젝션 탐지 시 차단 ──────────────────────────────────
    blocked = (
        bool(injection_items) and config.INJECTION_POLICY == "block"
    ) or (forced_snapshot.active and truncated)
    # 이미지 속 글자 검사가 켜졌는데 글자 검사 대상 이미지를 못 읽었으면(GPU·모델 없음, 실패) 보내지 않는다(사용자 결정).
    # 이미지 글이 든 문서가 잘렸으면 이미지 글 일부가 검사 밖이라 같은 이유로 막는다.
    if images_unchecked or (truncated and images_read):
        blocked = True
        reason = reason or (next((i["reason"] for i in image_infos if i["status"] == "unchecked"), image_ocr.UNCHECKED_REASON)
                            if images_unchecked
                            else "문서가 길어 이미지 속 글자 일부를 검사하지 못해 보내지 않습니다")
    confidential_items = [] if blocked else await confidential.detect(
        text,
        list(pii_items) + list(forced_items) + list(injection_items),
        emit,
        forced_snapshot,
        pii_keep=pii_keep,
    )
    if confidential_items:
        pii_items = _dedupe(list(pii_items) + confidential_items)

    # ── Step 5: 마스킹 ────────────────────────────────────────────────────────
    await emit({"type": "step", "step": 5, "label": "마스킹 적용 중..."})
    masked = masker.apply_masking(
        text, list(pii_items) + list(injection_items) + list(forced_items)
    )
    masked_text = masked["masked_text"]

    masked_file = None
    if wrap_file and not blocked:  # 이미지 파일(캡처)도 읽은 글을 가린 문서로 낸다 — 다른 문서와 같은 길(사용자 결정)
        wrap_name = "document" + os.path.splitext(file_name)[1] if name_forced else file_name
        wrapped = docwrapper.wrap_masked_file(masked_text, wrap_name)
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
        forced_mask_items=forced_items,
        scan_status=scan_status,
        reason=reason,
        blocked=blocked,
        masked_file=masked_file,
        truncated=truncated,
        original_chars=original_chars,
        user_prompt=user_prompt,
        user_prompt_masked=user_prompt_masked,
        user_prompt_pii_items=user_prompt_pii_items,
        user_prompt_forced_mask_items=user_prompt_forced_items,
        forced_policy=forced_policy,
        images=image_infos,
        images_removed=images_removed,
    )


def _build_result(
    *,
    original_text: str,
    masked_text: str,
    pii_items: list[Detection],
    injection_items: list[Detection],
    forced_mask_items: list[Detection],
    scan_status: str,
    reason: str | None,
    blocked: bool,
    masked_file: dict | None,
    truncated: bool = False,
    original_chars: int | None = None,
    user_prompt: str | None = None,
    user_prompt_masked: str | None = None,
    user_prompt_pii_items: list[Detection] | None = None,
    user_prompt_forced_mask_items: list[Detection] | None = None,
    forced_policy: dict[str, Any] | None = None,
    images: list[dict[str, Any]] | None = None,
    images_removed: int = 0,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        # originalText 는 세션 중 반환용(HITL diff). store 에는 저장 금지(PLAN §9.1).
        "originalText": original_text,
        # 차단 결과의 maskedText 는 원문이거나 덜 가린 텍스트일 수 있다(규칙 미수신·파싱 실패).
        # blocked 를 안 보는 소비자가 "마스킹본" 으로 내보내지 않게 비운다(MCP 응답과 같은 규칙).
        "maskedText": "" if blocked else masked_text,
        "piiItems": pii_items,
        "injectionItems": injection_items,
        "forcedMaskItems": forced_mask_items,
        # 상한(config.MAX_TEXT_CHARS)을 넘겨 앞부분만 검사했는가. 소비자는 이 값으로
        # "전부 검사했다" 와 "일부만 검사했다" 를 구분한다.
        #
        # truncated 만으로는 "얼마나 못 봤는지" 를 못 보여준다. 검토 UI 가
        # "검사 200,000자 / 전체 N자" 를 띄우려면 자르기 "전" 길이가 필요해서
        # 두 값을 함께 낸다. original_chars 를 안 넘긴 호출은 자른 적이 없다는 뜻이라
        # 검사 길이와 같은 값으로 채운다.
        "truncated": truncated,
        "scannedChars": len(original_text),
        "originalChars": original_chars if original_chars is not None else len(original_text),
        "scanStatus": scan_status,
        "reason": reason,
        "blocked": blocked,
        "policy": {
            "injection": config.INJECTION_POLICY,
            "forcedMask": forced_policy or {
                "managed": False, "ready": True, "active": False,
                "count": 0, "revision": "", "complete": True,
            },
        },
        "stats": {
            "piiCount": len(pii_items),
            "injectionCount": len(injection_items),
            "forcedMaskCount": len(forced_mask_items),
            "originalLength": len(original_text),
        },
    }
    if masked_file is not None:
        result["maskedFile"] = masked_file
    if images:  # 이미지 속 글자 검사: 이미지마다 읽음/건너뜀/미검사와 본문 속 위치(화면에 "이미지에서 읽은 글" 표시용)
        result["images"] = images
    if images_removed:  # 이미지 속 글자 검사가 꺼져 검사하지 않은 문서 속 그림 수 — 원본 대신 글만 보낸다
        result["imagesRemoved"] = images_removed
    if user_prompt is not None:
        # 문서와 "함께" 넘어온 실제 사용자 프롬프트의 PII 스캔 결과 — 확장 프로그램이
        # 문서 첨부를 보류했다가 프롬프트 전송 시점에 함께 넘긴 경우에만 채워진다.
        result["userPromptOriginal"] = user_prompt
        result["userPromptMasked"] = "" if blocked else user_prompt_masked
        result["userPromptPiiItems"] = user_prompt_pii_items or []
        result["userPromptForcedMaskItems"] = user_prompt_forced_mask_items or []
    return result
