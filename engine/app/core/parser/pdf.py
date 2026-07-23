"""
app/core/parser/pdf.py
PDF 파싱 — pdfplumber. 파이프라인/server/pipeline/parsers/pdf.py 이식.

표 영역은 find_tables() 로 먼저 탐지, filter() 로 일반 텍스트에서 제외 후
각 table_obj.extract() 로 별도 추출 → 중복 방지.
스캔(이미지) PDF 는 v1 미지원 → 사전 판별해 NotImplementedError.
"""

from __future__ import annotations

import io


def _looks_scanned(file_bytes: bytes) -> bool:
    """PyMuPDF 로 텍스트가 거의 없으면 스캔 PDF 로 간주 (PLAN §6, 사전 판별)."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return False  # PyMuPDF 없으면 판별 생략, pdfplumber 결과에 위임
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            total = sum(len((page.get_text() or "").strip()) for page in doc)
        return total < 10
    except Exception:  # noqa: BLE001
        return False


def extract_pdf(file_bytes: bytes) -> str:
    import pdfplumber

    if _looks_scanned(file_bytes):
        raise NotImplementedError("스캔(이미지) PDF 는 지원하지 않습니다 (v1 미지원).")

    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            parts: list[str] = []

            table_objects = page.find_tables()
            table_bboxes = [t.bbox for t in table_objects]

            if table_bboxes:
                def not_in_table(obj, bboxes=table_bboxes):
                    for x0, top, x1, bottom in bboxes:
                        if (
                            obj.get("x0", 0) >= x0
                            and obj.get("top", 0) >= top
                            and obj.get("x1", 0) <= x1
                            and obj.get("bottom", 0) <= bottom
                        ):
                            return False
                    return True

                text = page.filter(not_in_table).extract_text() or ""
            else:
                text = page.extract_text() or ""

            if text.strip():
                parts.append(text.strip())

            for table_obj in table_objects:
                for row in table_obj.extract():
                    cells = [str(c).strip() for c in row if c and str(c).strip()]
                    if cells:
                        parts.append(" | ".join(cells))

            page_content = "\n".join(parts).strip()
            if page_content:
                pages.append(page_content)

    if not pages:
        raise NotImplementedError(
            "PDF 에서 텍스트를 추출할 수 없습니다. 스캔(이미지) PDF 는 지원하지 않습니다."
        )

    return "\n\n---\n\n".join(pages)
