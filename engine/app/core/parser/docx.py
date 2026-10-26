"""
app/core/parser/docx.py
DOCX 파싱 — python-docx. 파이프라인/server/pipeline/parsers/docx.py 이식.

문단과 표를 문서에 나온 순서대로 꺼낸다. 예전엔 문단을 다 꺼낸 뒤 표를 맨 끝에 몰아 붙여, 표가 본문 어디에
있었는지 사라졌다(앞뒤 문단과 떨어져 기밀 판정·AI 답이 맥락을 잃었다).
"""

from __future__ import annotations

import io


def extract_docx(file_bytes: bytes, images: list[bytes] | None = None) -> str:
    """images 를 주면(이미지 속 글자 검사가 켜졌을 때) 그림이 든 문단(표 칸 안 포함) 자리에 표시를 남기고 그림을 담는다
    (parser/images.py)."""
    from docx import Document
    from docx.table import Table

    doc = Document(io.BytesIO(file_bytes))
    blocks: list[str] = []
    for item in doc.iter_inner_content():
        if isinstance(item, Table):
            blocks += _table_rows(item, images)
        elif text := _paragraph_text(item, images):
            blocks.append(text)
    return "\n\n".join(blocks)


def _paragraph_text(p, images: list[bytes] | None) -> str:
    text = p.text if p.text.strip() else ""
    if images is not None:
        from .images import docx_paragraph_images, mark

        marks = "".join(mark(images, blob) for blob in docx_paragraph_images(p))
        text = f"{text}\n{marks}".strip("\n") if marks else text
    return text


def _table_rows(table, images: list[bytes] | None) -> list[str]:
    """행마다 "칸 | 칸". 병합된 칸은 python-docx 가 칸 수만큼 같은 칸을 돌려주므로 처음 나온 자리에만 적는다."""
    rows, seen = [], set()
    for row in table.rows:
        cells = []
        for cell in row.cells:
            if cell._tc in seen:
                continue
            seen.add(cell._tc)
            text = "\n".join(t for p in cell.paragraphs if (t := _paragraph_text(p, images).strip()))
            if text:
                cells.append(text)
        if cells:
            rows.append(" | ".join(cells))
    return rows
