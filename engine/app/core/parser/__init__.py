"""
app/core/parser/
포맷별 파서 분기 (PLAN §6). U1: TXT / PDF / DOCX. U5: HWP / HWPX / XLSX / PPTX.
XLS / PPT(구버전 바이너리)는 범위 밖 → 스텁(미지원 반환).

parse_document() 는 예외를 던지지 않고 (text, scan_status, reason) 을 돌려
"미검사 통과" 정책(PLAN §9.2)을 파이프라인이 일관되게 처리하게 한다.
"""

from __future__ import annotations

import os

from app import config

# scan_status 값 (PLAN §9.2)
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_UNSUPPORTED = "unsupported"
STATUS_TIMEOUT = "timeout"


def _ext(file_name: str) -> str:
    return os.path.splitext(file_name or "")[1].lower()


def parse_document(file_bytes: bytes, mime_type: str, file_name: str) -> tuple[str, str, str | None]:
    """포맷 분기 후 텍스트 추출.

    반환: (text, scan_status, reason)
      - 성공: (추출텍스트, "ok", None)
      - 미지원: ("", "unsupported", 사유)
      - 파싱 실패: ("", "failed", 사유)
    예외를 밖으로 던지지 않는다(PLAN §9.2 미검사 통과).
    """
    ext = _ext(file_name)

    # U5 미지원 포맷 — 스텁 (PLAN §6)
    if ext in config.UNSUPPORTED_EXTENSIONS:
        from .stubs import unsupported_reason

        return "", STATUS_UNSUPPORTED, unsupported_reason(ext)

    try:
        if ext == ".txt" or mime_type.startswith("text/"):
            from .txt import extract_txt

            return extract_txt(file_bytes), STATUS_OK, None

        if ext == ".pdf" or mime_type == "application/pdf":
            from .pdf import extract_pdf

            return extract_pdf(file_bytes), STATUS_OK, None

        if ext == ".docx" or "wordprocessingml" in mime_type:
            from .docx import extract_docx

            return extract_docx(file_bytes), STATUS_OK, None

        if ext == ".xlsx" or "spreadsheetml" in mime_type:
            from .xlsx import extract_xlsx

            return extract_xlsx(file_bytes), STATUS_OK, None

        if ext == ".pptx" or "presentationml" in mime_type:
            from .pptx import extract_pptx

            return extract_pptx(file_bytes), STATUS_OK, None

        if ext == ".hwpx":
            from .hwpx import extract_hwpx

            return extract_hwpx(file_bytes), STATUS_OK, None

        if ext == ".hwp" or mime_type == "application/x-hwp":
            from .hwp import extract_hwp

            return extract_hwp(file_bytes), STATUS_OK, None

    except NotImplementedError as exc:
        # 스캔 PDF 등 v1 미지원 케이스
        return "", STATUS_UNSUPPORTED, str(exc)
    except Exception as exc:  # noqa: BLE001 - 정책상 절대 죽지 않음 (PLAN §9.2)
        return "", STATUS_FAILED, f"파서 오류: {exc}"

    # 확장자/MIME 모두 매칭 안 됨 → txt 로 폴백 시도
    try:
        from .txt import extract_txt

        return extract_txt(file_bytes), STATUS_OK, None
    except Exception:  # noqa: BLE001
        return "", STATUS_UNSUPPORTED, f"지원하지 않는 포맷: {ext or mime_type}"
