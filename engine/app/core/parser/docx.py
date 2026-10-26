"""
app/core/parser/docx.py
DOCX 파싱 — python-docx. 파이프라인/server/pipeline/parsers/docx.py 이식.
"""

from __future__ import annotations

import io


def extract_docx(file_bytes: bytes, images: list[bytes] | None = None) -> str:
    """images 를 주면(이미지 속 글자 검사가 켜졌을 때) 그림이 든 문단 자리에 표시를 남기고 그림을 담는다(parser/images.py)."""
    from docx import Document

    doc = Document(io.BytesIO(file_bytes))
    paragraphs = []
    for p in doc.paragraphs:
        text = p.text if p.text.strip() else ""
        if images is not None:
            from .images import docx_paragraph_images, mark

            marks = "".join(mark(images, blob) for blob in docx_paragraph_images(p))
            text = f"{text}\n{marks}".strip("\n") if marks else text
        if text:
            paragraphs.append(text)

    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                paragraphs.append(" | ".join(cells))

    return "\n\n".join(paragraphs)
