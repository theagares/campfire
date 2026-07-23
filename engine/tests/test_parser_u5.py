"""
tests/test_parser_u5.py
U5(파서 확장: HWP/HWPX/XLSX/PPTX) 통합 테스트 (PLAN §6, §9.2, §10 Phase 5).

- XLSX/PPTX: openpyxl/python-pptx 로 실제 샘플을 생성해 parse_document + 전체
  파이프라인(run_pipeline)까지 실 PII 탐지/마스킹이 되는지 검증.
- HWPX: 이 환경에 한/글(한컴오피스)이 설치돼 있으면 pyhwpx 로 실제 hwpx 샘플을
  생성해 왕복 검증, 없으면 "미지원 통과" 경로만 검증.
- HWP: LibreOffice 미설치 환경에서 "우아한 미지원 통과"(PLAN §9.2, §11)를 검증.
  (LibreOffice 가 설치된 환경이면 실제 변환 경로까지 확인.)
"""

from __future__ import annotations

import asyncio
import io
import os
import tempfile

import pytest

from app.core.parser import STATUS_FAILED, STATUS_OK, STATUS_UNSUPPORTED, parse_document
from app.core.parser import hwp as hwp_module
from app.core.pipeline.orchestrator import run_pipeline

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


# ── XLSX ──────────────────────────────────────────────────────────────────────


def _make_xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "고객목록"
    ws.append(["구분", "메모"])
    # PERSON_NAME 룰(app/rules/pii_ko.yaml)은 "성명/이름/고객명 : 값" 앵커가
    # 같은 텍스트 런 안에 있어야 매치되므로, 헤더/값을 별도 셀로 나누지 않고
    # 한 셀 안에 라벨+값을 함께 넣는다(실제 스프레드시트의 "메모" 컬럼과 유사).
    ws.append(["신규 고객", "성명: 홍길동, 이메일: hong@example.com, 연락처: 010-1234-5678"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_parse_extracts_all_sheet_text():
    text, status, reason = parse_document(_make_xlsx_bytes(), XLSX_MIME, "customers.xlsx")
    assert status == STATUS_OK
    assert reason is None
    assert "홍길동" in text
    assert "hong@example.com" in text
    assert "010-1234-5678" in text


def test_xlsx_pipeline_detects_and_masks_pii():
    result = asyncio.run(
        run_pipeline(file_bytes=_make_xlsx_bytes(), mime_type=XLSX_MIME, file_name="customers.xlsx")
    )
    assert result["scanStatus"] == STATUS_OK
    assert result["stats"]["piiCount"] >= 3  # 이름 + 이메일 + 연락처
    assert "홍길동" not in result["maskedText"]
    assert "hong@example.com" not in result["maskedText"]
    assert "010-1234-5678" not in result["maskedText"]


# ── PPTX ──────────────────────────────────────────────────────────────────────


def _make_pptx_bytes() -> bytes:
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "고객 정보"
    body = slide.placeholders[1]
    body.text_frame.text = "성명: 김철수"
    p = body.text_frame.add_paragraph()
    p.text = "연락처: 010-9876-5432 / 이메일: kim@example.com"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_pptx_parse_extracts_all_slide_text():
    text, status, reason = parse_document(_make_pptx_bytes(), PPTX_MIME, "briefing.pptx")
    assert status == STATUS_OK
    assert reason is None
    assert "김철수" in text
    assert "010-9876-5432" in text
    assert "kim@example.com" in text


def test_pptx_pipeline_detects_and_masks_pii():
    result = asyncio.run(
        run_pipeline(file_bytes=_make_pptx_bytes(), mime_type=PPTX_MIME, file_name="briefing.pptx")
    )
    assert result["scanStatus"] == STATUS_OK
    assert result["stats"]["piiCount"] >= 2
    assert "김철수" not in result["maskedText"]
    assert "kim@example.com" not in result["maskedText"]


# ── HWP (LibreOffice 변환) ────────────────────────────────────────────────────


def test_hwp_graceful_unsupported_or_real_conversion():
    """LibreOffice 미설치 환경 → unsupported 로 우아하게 통과(PLAN §9.2/§11).

    LibreOffice 가 설치돼 있으면(예외적) 더미 바이트는 진짜 HWP가 아니므로
    변환 실패 → failed 로 통과하는 것도 정상(§9.2 "파싱 실패"도 동일 정책).
    """
    result = asyncio.run(
        run_pipeline(
            file_bytes=b"this is not a real hwp binary",
            mime_type="application/x-hwp",
            file_name="report.hwp",
        )
    )

    if hwp_module._find_soffice() is None:
        assert result["scanStatus"] == STATUS_UNSUPPORTED
        assert "LibreOffice" in (result["reason"] or "")
    else:
        assert result["scanStatus"] in (STATUS_UNSUPPORTED, STATUS_FAILED)

    assert result["stats"]["piiCount"] == 0  # 검사 자체를 안 함(§9.2)


def test_hwp_find_soffice_returns_none_or_path():
    # 탐색 함수 자체가 예외 없이 동작하는지만 확인(있으면 str, 없으면 None)
    found = hwp_module._find_soffice()
    assert found is None or isinstance(found, str)


# ── HWPX (pyhwpx) ─────────────────────────────────────────────────────────────


def _try_make_real_hwpx_bytes() -> bytes | None:
    """이 머신에 한/글(한컴오피스)이 설치돼 있으면 실제 hwpx 샘플을 생성해서 반환.
    설치돼 있지 않으면 None(그 경우 미지원 통과 경로만 검증)."""
    try:
        from pyhwpx import Hwp
    except ImportError:
        return None

    fd, path = tempfile.mkstemp(suffix=".hwpx")
    os.close(fd)
    os.remove(path)

    hwp = None
    try:
        hwp = Hwp(new=True, visible=False, register_module=True)
    except Exception:
        return None

    try:
        hwp.insert_text("성명: 박영희")
        hwp.insert_text(" 이메일: park@example.com")
        ok = hwp.save_as(path, format="HWPX")
        if not ok or not os.path.exists(path):
            return None
    except Exception:
        return None
    finally:
        try:
            hwp.quit(save=False)
        except Exception:
            pass

    try:
        with open(path, "rb") as f:
            data = f.read()
        return data
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_hwpx_real_extraction_or_graceful_unsupported():
    hwpx_bytes = _try_make_real_hwpx_bytes()

    if hwpx_bytes is None:
        # 한/글 미설치 또는 샘플 생성 실패 → 최소한 미지원 처리 경로만 검증
        result = asyncio.run(
            run_pipeline(
                file_bytes=b"not a real hwpx",
                mime_type="application/octet-stream",
                file_name="sample.hwpx",
            )
        )
        assert result["scanStatus"] in (STATUS_UNSUPPORTED, STATUS_FAILED)
        assert result["stats"]["piiCount"] == 0
        return

    text, status, reason = parse_document(hwpx_bytes, "application/octet-stream", "sample.hwpx")
    assert status == STATUS_OK, f"실제 한/글 설치 환경에서 HWPX 파싱 실패: {reason}"
    assert "박영희" in text
    assert "park@example.com" in text

    result = asyncio.run(
        run_pipeline(file_bytes=hwpx_bytes, mime_type="application/octet-stream", file_name="sample.hwpx")
    )
    assert result["scanStatus"] == STATUS_OK
    assert result["stats"]["piiCount"] >= 1
    assert "박영희" not in result["maskedText"]
