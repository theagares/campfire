"""
app/adapters/http_api/
REST 어댑터 (PLAN §2). 기존 EC2 API 계약 호환:
  GET  /health              (+ 시그니처 필드, PLAN §11)
  POST /jobs                파일 업로드 → 마스킹 결과
  POST /jobs/prompt         텍스트 → 마스킹 텍스트
  GET  /jobs/{id}/events    진행 이벤트 (폴링 JSON 또는 SSE)
"""

from fastapi import APIRouter

from . import events, health, jobs

router = APIRouter()
router.include_router(health.router)
router.include_router(jobs.router)
router.include_router(events.router)
