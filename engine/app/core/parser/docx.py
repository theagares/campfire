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
    """행마다 "칸 | 칸". 중첩 표도 셀 안의 원래 위치에서 재귀 처리한다.

    병합된 칸은 python-docx 가 칸 수만큼 같은 칸을 돌려준다. 가로 병합은 한 행에서
    한 번만 적고, 세로 병합의 다음 행에는 빈 칸을 남겨 뒤 셀이 왼쪽으로 당겨지지
    않게 한다.
    """
    from docx.table import Table

    rows, seen = [], set()
    for row in table.rows:
        cells, row_seen = [], set()
        for cell in row.cells:
            if cell._tc in row_seen:  # 가로 병합: 같은 행에서 반복되는 칸
                continue
            row_seen.add(cell._tc)
            if cell._tc in seen:  # 세로 병합: 내용은 반복하지 말고 열 자리만 유지
                cells.append("")
                continue
            seen.add(cell._tc)

            parts: list[str] = []
            for item in cell.iter_inner_content():
                if isinstance(item, Table):
                    parts.extend(_table_rows(item, images))
                elif text := _paragraph_text(item, images).strip():
                    parts.append(text)
            cells.append("\n".join(parts))
        if any(cells):
            rows.append(" | ".join(cells))
    return rows
