"""
app/core/parser/pptx.py
PPTX 파싱 — python-pptx (PLAN §6).

모든 슬라이드의 텍스트 프레임(+표)을 화면의 위→아래·왼쪽→오른쪽 순서로
순회해 하나의 문서 텍스트로 합친다. slide.shapes 의 원래 순서는 화면 위치가
아니라 도형 생성/z-order 순서라서 그대로 돌면 아래쪽 글이 먼저 나올 수 있다.
"""

from __future__ import annotations

import io


def _shape_position(shape) -> tuple[int, int, int]:
    """좌표가 없는 특수 도형도 안정적으로 정렬한다."""
    return (
        int(getattr(shape, "top", 0) or 0),
        int(getattr(shape, "left", 0) or 0),
        int(getattr(shape, "shape_id", 0) or 0),
    )


def _table_rows(table) -> list[str]:
    rows: list[str] = []
    for row in table.rows:
        cells = [cell.text.strip() for cell in row.cells]
        if any(cells):
            rows.append(" | ".join(cells))
    return rows


def _shape_parts(shape, images: list[bytes] | None) -> list[str]:
    """도형 하나의 글/표/그림을 반환한다. 묶음 도형은 내부 좌표순으로 재귀 처리."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        return [
            part
            for child in sorted(shape.shapes, key=_shape_position)
            for part in _shape_parts(child, images)
        ]

    parts: list[str] = []
    if images is not None:
        from .images import mark, pptx_shape_images

        parts.extend(mark(images, blob) for blob in pptx_shape_images(shape))
    if shape.has_text_frame:
        text = "\n".join(
            paragraph.text.strip()
            for paragraph in shape.text_frame.paragraphs
            if paragraph.text.strip()
        )
        if text:
            parts.append(text)
    if shape.has_table:
        parts.extend(_table_rows(shape.table))
    return parts


def extract_pptx(file_bytes: bytes, images: list[bytes] | None = None) -> str:
    """images 를 주면(이미지 속 글자 검사가 켜졌을 때) 그림 도형 자리에 표시를 남기고 그림을 담는다(parser/images.py)."""
    from pptx import Presentation

    prs = Presentation(io.BytesIO(file_bytes))

    slides: list[str] = []
    for idx, slide in enumerate(prs.slides, start=1):
        parts = [
            part
            for shape in sorted(slide.shapes, key=_shape_position)
            for part in _shape_parts(shape, images)
        ]
        if parts:
            slides.append(f"[슬라이드 {idx}]\n" + "\n".join(parts))

    return "\n\n---\n\n".join(slides)
