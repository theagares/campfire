"""
app/adapters/http_api/jobs.py
POST /jobs        — 파일 업로드 → 마스킹 결과
POST /jobs/prompt — 텍스트(프롬프트) → PII/강제 마스킹 텍스트

기존 EC2 계약 호환: 응답에 jobId 포함. 룰베이스 v1 은 즉시 처리되므로
결과(result)는 HTTP 응답으로 한 번만 반환한다. /jobs/{id}/events 에는 진행 상태와
완료 표식만 남겨 큰 원문·마스킹 파일을 메모리에 중복 보관하지 않는다.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app import config
from app.core import activity as activity_bus
from app.core.pipeline.orchestrator import run_pipeline
from app.store import db

from . import job_registry

router = APIRouter()
logger = logging.getLogger("securedoc.engine")


async def _fail(job_id: str, exc: Exception) -> None:
    """탐지/마스킹 단계에서 예외 발생 시: 서버 로그에 원인을 남기고(디버깅용),
    job 이벤트도 error 로 마감한 뒤, 클라이언트에는 원인이 보이는 500 을 준다.

    파싱 실패와 달리(§9.2 미검사 통과), 이 단계는 텍스트 추출은 이미 끝난
    상태라 예외를 삼키고 원문을 그대로 반환하면 마스킹 없이 새는 것과 같다
    (silent PII leak) — 그래서 여기는 "미검사 통과"가 아니라 명확히 실패로
    응답한다.
    """
    logger.exception("파이프라인 처리 중 오류 (job=%s)", job_id)
    # activity=True 로 방송까지 해야 처리현황의 "탐지중" 이 풀린다 — 실패한 job 이
    # 방송에서 안 끝나면 대시보드가 영원히 처리 중으로 남는다.
    await job_registry.make_emit(job_id, activity=True)({"type": "error", "message": str(exc)})
    raise HTTPException(status_code=500, detail=f"파이프라인 처리 중 오류: {exc}") from exc


def _record(job_id: str, *, file_name: str, source: str, result: dict) -> None:
    """탐지 통계 기록. **실패해도 응답을 깨지 않는다.**

    완료 이벤트보다 먼저 기록해야 그 이벤트를 받은 데스크톱의 통계 재조회에 방금 건이
    포함된다. 기록은 통계용이고 마스킹 결과와 무관하므로 디스크가 가득 차거나 항목
    모양이 예상과 달라도 사용자 흐름을 막지 않고 완료 이벤트와 HTTP 응답은 그대로 낸다.
    """
    try:
        db.record_job(job_id, file_name=file_name, source=source, result=result)
    except Exception:  # noqa: BLE001 - 기록 실패로 마스킹 결과를 버리지 않는다
        logger.exception("job 기록 실패 (job=%s) — 응답은 그대로 진행", job_id)


@router.post("/jobs/prompt")
async def create_prompt_job(text: str = Form(...)):
    """사용자 프롬프트는 PII와 강제 규칙만 검사하고 인젝션 검사는 하지 않는다."""
    if len(text) > config.MAX_PROMPT_CHARS:
        raise HTTPException(status_code=413, detail="prompt too long")

    job_id = str(uuid.uuid4())
    job_registry.create_job(job_id)
    activity_bus.job_started(job_id, source="prompt")
    emit = job_registry.make_emit(job_id, activity=True)

    try:
        # 확장이 AI 사이트 입력창에서 가로챈 사용자 본인의 프롬프트다 — 문서처럼 "AI 에게
        # 내리는 지시 = 인젝션" 으로 보면 평범한 요청이 통째로 가려진다. 이 경로는
        # 인젝션 검사를 생략하고 PII·자격증명·강제 마스킹만 수행한다.
        result = await run_pipeline(
            text=text,
            file_name="prompt.txt",
            emit=emit,
            wrap_file=False,
            direct_prompt=True,
            scan_injection=False,
        )
    except Exception as exc:  # noqa: BLE001 - 아래에서 로그 남기고 500 으로 변환
        await _fail(job_id, exc)
    _record(job_id, file_name="prompt.txt", source="prompt", result=result)
    await emit({"type": "done"})
    return {"jobId": job_id, "done": True, "result": _public(result)}


@router.post("/jobs")
async def create_job(
    file: UploadFile = File(...),
    mimeType: str = Form(""),
    fileName: str = Form(""),
    userPrompt: str = Form(""),
    wrapFile: bool = Form(True),
):
    """userPrompt: 문서와 함께 사용자가 실제로 보내려는 프롬프트(선택).

    확장 프로그램이 문서 첨부를 곧바로 스캔하지 않고 사용자가 프롬프트를 보낼
    때까지 보류했다가, 전송 시점에 파일과 함께 넘기는 시나리오에서 채워진다.
    주어지면 인젝션 탐지가 placeholder 대신 이 실제 프롬프트를 근거로 판단하고,
    프롬프트 자체도 PII 스캔해 결과에 포함한다(userPromptMasked/PiiItems).

    wrapFile: 마스킹본 파일(maskedFile)을 만들어 응답에 실을지. 기본 True 라
    기존 단일 첨부 경로는 그대로다. 다중 첨부는 False 로 부른다 — 검사 시점에는
    사용자가 아직 아무 마스킹도 해제하지 않아 "최종본" 이 정해지지 않았고,
    N개의 base64 를 세션에 쌓지 않으려는 것이다. 최종 파일은 사용자의 결정이
    끝난 뒤 확장(SW)이 한 번만 만든다.
    """
    file_bytes = await file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(file_bytes) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")
    if userPrompt and len(userPrompt) > config.MAX_PROMPT_CHARS:
        raise HTTPException(status_code=413, detail="prompt too long")

    name = fileName or file.filename or "upload.bin"
    mime = mimeType or file.content_type or ""

    job_id = str(uuid.uuid4())
    job_registry.create_job(job_id)
    activity_bus.job_started(job_id, source="extension")
    emit = job_registry.make_emit(job_id, activity=True)

    try:
        result = await run_pipeline(
            file_bytes=file_bytes,
            mime_type=mime,
            file_name=name,
            emit=emit,
            wrap_file=wrapFile,
            user_prompt=userPrompt or None,
        )
    except Exception as exc:  # noqa: BLE001 - 아래에서 로그 남기고 500 으로 변환
        await _fail(job_id, exc)
    _record(job_id, file_name=name, source="extension", result=result)
    await emit({"type": "done"})
    return {"jobId": job_id, "done": True, "result": _public(result)}


def _public(result: dict) -> dict:
    """API 응답용 뷰. originalText 는 세션 내 diff 용으로만 포함(디스크 미저장은 store 책임)."""
    return result
