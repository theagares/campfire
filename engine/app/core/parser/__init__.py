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


def _looks_textual(file_bytes: bytes, mime_type: str) -> bool:
    """이 바이트열을 텍스트로 읽어도 되는지."""
    if mime_type.startswith("text/"):
        return True
    sample = file_bytes[:8192]
    if not sample:
        return False
    if 0 in sample:
        return False  # NUL 바이트는 바이너리라는 가장 확실한 신호
    for enc in ("utf-8", "cp949"):
        try:
            sample.decode(enc)
            return True
        except UnicodeDecodeError as exc:
            # 8KB 로 자르느라 마지막 글자가 반토막 난 것뿐이면 텍스트로 본다.
            if exc.start >= len(sample) - 4:
                return True
    return False


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

    # 확장자/MIME 모두 매칭 안 됨.
    #
    # 예전엔 무조건 txt 로 폴백했다. 그런데 extract_txt 는 최후에 errors="replace" 로
    # **무조건 성공**한다 — zip/png/exe 가 깨진 문자열과 함께 STATUS_OK 로 나왔다.
    # 사용자에겐 정상 검사로 보이지만 실제로 검사된 건 모지바케다. 파싱 실패
    # (failed/unsupported)는 사이드패널이 "이 입력은 검사되지 않았습니다" 라고 정직하게
    # 알리는데 이 경로만 그 고지를 우회했다. 모르면 "검사했다" 고 하지 않는다.
    if _looks_textual(file_bytes, mime_type):
        try:
            from .txt import extract_txt

            return extract_txt(file_bytes), STATUS_OK, None
        except Exception:  # noqa: BLE001
            pass
    return "", STATUS_UNSUPPORTED, f"지원하지 않는 포맷: {ext or mime_type or '알 수 없음'}"
