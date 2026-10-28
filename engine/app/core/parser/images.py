"""
app/core/parser/images.py
파일 속 이미지 꺼내기 — 이미지 속 글자 검사(config.IMAGE_OCR_ENABLED)가 켜져 있을 때만 쓴다.

파서는 본문의 이미지 자리에 표시(MARK: 사용 영역 문자 + 번호)를 남기고 이미지 바이트를 목록에 담는다.
orchestrator 가 VARCO OCR 로 읽은 글로 표시를 바꾼다(core/detectors/image_ocr.py). 꺼져 있으면 파서는
목록을 받지 않고 지금과 똑같은 글을 낸다.
ponytail: XLSX·HWPX·PDF 속 이미지와 이미지로만 된 PDF 쪽은 아직 꺼내지 않는다 — 필요해지면 여기에 더한다.
"""

from __future__ import annotations

import re

from app.config import IMAGE_OCR_EXTENSIONS as IMAGE_EXTENSIONS

IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/bmp"})
MARK_RE = re.compile("(\\d+)")
_MARK_PARAGRAPH_RE = re.compile(r"(?:\n\n)?" + MARK_RE.pattern)
_BLIP_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"


def is_image(ext: str, mime_type: str) -> bool:
    return ext in IMAGE_EXTENSIONS or mime_type in IMAGE_MIMES


def strip_marks(text: str) -> str:
    """이미지 표시를 지운 글. 이미지 속 글자 검사가 꺼져 있을 때 — 그림을 세기만 하고 글은 지금과 같게 둔다.
    그림만 든 문단은 표시가 문단 하나를 차지하므로 앞의 문단 구분도 함께 지운다(안 그러면 빈 줄이 늘어난다)."""
    return _MARK_PARAGRAPH_RE.sub("", text)


def mark(images: list[bytes], blob: bytes) -> str:
    """이미지를 목록에 넣고 본문에 남길 표시를 돌려준다."""
    images.append(blob)
    return f"{len(images) - 1}"


def docx_paragraph_images(paragraph) -> list[bytes]:
    """문단 안 그림(본문 속·떠 있는 그림 모두 a:blip)의 바이트."""
    out = []
    for blip in paragraph._p.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}blip"):
        rid = blip.get(_BLIP_EMBED)
        part = paragraph.part.related_parts.get(rid) if rid else None
        if part is not None and getattr(part, "blob", None):
            out.append(part.blob)
    return out


def pptx_shape_images(shape) -> list[bytes]:
    """그림 도형(묶음 안 포함)의 바이트."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        return [b for s in shape.shapes for b in pptx_shape_images(s)]
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        try:
            return [shape.image.blob]
        except (AttributeError, ValueError, KeyError):  # 연결만 된(외부) 그림
            return []
    return []
