"""
app/adapters/http_api/jobs.py
POST /jobs        — 파일 업로드 → 마스킹 결과
POST /jobs/prompt — 텍스트(프롬프트) → 마스킹 텍스트

기존 EC2 계약 호환: 응답에 jobId 포함. 룰베이스 v1 은 즉시 처리되므로
결과(result)도 인라인으로 함께 반환한다(curl 한 방으로 마스킹 결과 확인 가능,
PLAN §10 Phase 0 완료 기준). 이벤트는 /jobs/{id}/events 로도 재생된다.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app import config
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
    await job_registry.make_emit(job_id)({"type": "error", "message": str(exc)})
    raise HTTPException(status_code=500, detail=f"파이프라인 처리 중 오류: {exc}") from exc


@router.post("/jobs/prompt")
async def create_prompt_job(text: str = Form(...)):
    if len(text) > config.MAX_PROMPT_CHARS:
        raise HTTPException(status_code=413, detail="prompt too long")

    job_id = str(uuid.uuid4())
    job_registry.create_job(job_id)
    emit = job_registry.make_emit(job_id)

    try:
        result = await run_pipeline(text=text, file_name="prompt.txt", emit=emit, wrap_file=False)
    except Exception as exc:  # noqa: BLE001 - 아래에서 로그 남기고 500 으로 변환
        await _fail(job_id, exc)
    await emit({"type": "done", "result": _public(result)})

    db.record_job(job_id, file_name="prompt.txt", source="prompt", result=result)
    return {"jobId": job_id, "done": True, "result": _public(result)}


@router.post("/jobs")
async def create_job(
    file: UploadFile = File(...),
    mimeType: str = Form(""),
    fileName: str = Form(""),
    userPrompt: str = Form(""),
):
    """userPrompt: 문서와 함께 사용자가 실제로 보내려는 프롬프트(선택).

    확장 프로그램이 문서 첨부를 곧바로 스캔하지 않고 사용자가 프롬프트를 보낼
    때까지 보류했다가, 전송 시점에 파일과 함께 넘기는 시나리오에서 채워진다.
    주어지면 인젝션 탐지가 placeholder 대신 이 실제 프롬프트를 근거로 판단하고,
    프롬프트 자체도 PII 스캔해 결과에 포함한다(userPromptMasked/PiiItems).
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
    emit = job_registry.make_emit(job_id)

    try:
        result = await run_pipeline(
            file_bytes=file_bytes,
            mime_type=mime,
            file_name=name,
            emit=emit,
            wrap_file=True,
            user_prompt=userPrompt or None,
        )
    except Exception as exc:  # noqa: BLE001 - 아래에서 로그 남기고 500 으로 변환
        await _fail(job_id, exc)
    await emit({"type": "done", "result": _public(result)})

    db.record_job(job_id, file_name=name, source="extension", result=result)
    return {"jobId": job_id, "done": True, "result": _public(result)}


def _public(result: dict) -> dict:
    """API 응답용 뷰. originalText 는 세션 내 diff 용으로만 포함(디스크 미저장은 store 책임)."""
    return result
