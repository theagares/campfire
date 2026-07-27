"""
app/adapters/http_api/jobs.py
POST /jobs        — 파일 업로드 → 마스킹 결과
POST /jobs/prompt — 텍스트(프롬프트) → 마스킹 텍스트

기존 EC2 계약 호환: 응답에 jobId 포함. 룰베이스 v1 은 즉시 처리되므로
결과(result)도 인라인으로 함께 반환한다(curl 한 방으로 마스킹 결과 확인 가능,
PLAN §10 Phase 0 완료 기준). 이벤트는 /jobs/{id}/events 로도 재생된다.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app import config
from app.core.pipeline.orchestrator import run_pipeline
from app.store import db

from . import job_registry

router = APIRouter()


@router.post("/jobs/prompt")
async def create_prompt_job(text: str = Form(...)):
    if len(text) > config.MAX_PROMPT_CHARS:
        raise HTTPException(status_code=413, detail="prompt too long")

    job_id = str(uuid.uuid4())
    job_registry.create_job(job_id)
    emit = job_registry.make_emit(job_id)

    result = await run_pipeline(text=text, file_name="prompt.txt", emit=emit, wrap_file=False)
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

    result = await run_pipeline(
        file_bytes=file_bytes,
        mime_type=mime,
        file_name=name,
        emit=emit,
        wrap_file=True,
        user_prompt=userPrompt or None,
    )
    await emit({"type": "done", "result": _public(result)})

    db.record_job(job_id, file_name=name, source="extension", result=result)
    return {"jobId": job_id, "done": True, "result": _public(result)}


def _public(result: dict) -> dict:
    """API 응답용 뷰. originalText 는 세션 내 diff 용으로만 포함(디스크 미저장은 store 책임)."""
    return result
