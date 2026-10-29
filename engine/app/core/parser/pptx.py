"""
app/core/parser/pptx.py
PPTX 파싱 — python-pptx (PLAN §6).

모든 슬라이드의 텍스트 프레임(+표)을 화면의 위→아래·왼쪽→오른쪽 순서로
순회해 하나의 문서 텍스트로 합친다. slide.shapes 의 원래 순서는 화면 위치가
아니라 도형 생성/z-order 순서라서 그대로 돌면 아래쪽 글이 먼저 나올 수 있다.

숨긴 글(parser/hidden.py): 숨긴 슬라이드(show="0")·숨긴 도형(hidden="1")·슬라이드 밖에 놓인 도형(맨 위 도형만 — 묶음 안
좌표는 묶음 기준이다)의 글, 1pt 이하 글자(sz ≤ 100), 글자색 = 도형 채움(없으면 슬라이드 → 레이아웃 → 마스터 배경)인 글자를
표식으로 감싼다. 색은 직접 RGB·테마 색(마스터 clrMap → 테마 clrScheme)만 푼다 — 밝기 변형(lumMod 등)·그라데이션·그림 배경은
모르는 색으로 보고 숨기지 않은 것으로 둔다. 글자색을 직접 정하지 않은 글자(자리 표시자 상속)도 보지 않는다.
"""

from __future__ import annotations

import io

from .hidden import color_reason, wrap

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _shape_position(shape) -> tuple[int, int, int]:
    """좌표가 없는 특수 도형도 안정적으로 정렬한다."""
    return (
        int(getattr(shape, "top", 0) or 0),
        int(getattr(shape, "left", 0) or 0),
        int(getattr(shape, "shape_id", 0) or 0),
    )


def _scheme(master) -> dict[str, str | None]:
    """테마 색 이름(dk1·lt1·accent1 …, 마스터 clrMap 의 bg1·tx1 …) → RGB."""
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.oxml import parse_xml

    try:
        theme = parse_xml(master.part.part_related_by(RT.THEME).blob)
    except Exception:  # noqa: BLE001 - 테마가 없거나 깨졌으면 테마 색을 모르는 것으로
        return {}
    colors: dict[str, str | None] = {}
    for el in theme.findall(f"{_A}themeElements/{_A}clrScheme/*"):
        if len(el):
            colors[el.tag.split("}")[1]] = el[0].get("val") if el[0].tag == f"{_A}srgbClr" else el[0].get("lastClr")
    cmap = master._element.find(f"{_P}clrMap")
    if cmap is not None:
        colors.update({k: colors.get(v) for k, v in cmap.attrib.items()})
    return colors


def _color(el, scheme: dict) -> str | None:
    """색 요소(a:srgbClr·a:schemeClr·a:sysClr) → RGB. 변형(lumMod·alpha 등 자식)이 있으면 모르는 색."""
    if el is None or len(el):
        return None
    tag = el.tag.split("}")[1]
    if tag == "srgbClr":
        return el.get("val")
    if tag == "schemeClr":
        return scheme.get(el.get("val"))
    if tag == "sysClr":
        return el.get("lastClr")
    return None


def _solid(parent, scheme: dict) -> str | None:
    fill = parent.find(f"{_A}solidFill") if parent is not None else None
    return _color(fill[0], scheme) if fill is not None and len(fill) else None


def _slide_bg(slide, scheme: dict) -> str | None:
    """슬라이드 → 레이아웃 → 마스터 순으로 처음 정한 배경색. 그라데이션·그림이면 None, 아무도 안 정했으면 흰색."""
    layout = slide.slide_layout
    for owner in (slide, layout, layout.slide_master):
        bg = owner._element.find(f"{_P}cSld/{_P}bg")
        if bg is None:
            continue
        pr, ref = bg.find(f"{_P}bgPr"), bg.find(f"{_P}bgRef")
        if pr is not None:
            return _solid(pr, scheme)
        return _color(ref[0], scheme) if ref is not None and len(ref) else None
    return "FFFFFF"


def _shape_fill(shape, slide_bg: str | None, scheme: dict) -> str | None:
    """글 뒤에 실제로 보이는 색: 도형 채움, 채움 없음이면 슬라이드 배경. 모르면 None."""
    sp_pr = shape._element.find(f"{_P}spPr")
    if sp_pr is not None:
        if sp_pr.find(f"{_A}noFill") is not None:
            return slide_bg
        if sp_pr.find(f"{_A}solidFill") is not None:
            return _solid(sp_pr, scheme)
        if any(sp_pr.find(f"{_A}{t}") is not None for t in ("gradFill", "blipFill", "pattFill", "grpFill")):
            return None
    ref = shape._element.find(f"{_P}style/{_A}fillRef")
    if ref is not None and ref.get("idx") not in (None, "0"):
        return _color(ref[0], scheme) if len(ref) else None
    return slide_bg


def _run_code(run, bg: str | None, scheme: dict) -> str | None:
    rpr = run.find(f"{_A}rPr")
    if rpr is None:
        return None
    if (rpr.get("sz") or "").isdigit() and int(rpr.get("sz")) <= 100:
        return "s"
    return color_reason(_solid(rpr, scheme), bg)


def _paragraph_text(paragraph, bg: str | None, scheme: dict) -> str:
    """paragraph.text 와 같은 글 — 숨긴 글자만 표식으로 감싼다."""
    parts, hidden = [], False
    for child in paragraph._element.content_children:
        text = child.text
        code = _run_code(child, bg, scheme) if text and child.tag in (f"{_A}r", f"{_A}fld") else None
        hidden = hidden or code is not None
        parts.append(wrap(text, code) if code else text)
    return "".join(parts) if hidden else paragraph.text


def _frame_text(frame, bg: str | None, scheme: dict) -> str:
    return "\n".join(
        _paragraph_text(paragraph, bg, scheme).strip()
        for paragraph in frame.paragraphs
        if paragraph.text.strip()
    )


def _table_rows(table, bg: str | None, scheme: dict) -> list[str]:
    rows: list[str] = []
    for row in table.rows:
        # cell.text 와 같게 빈 문단까지 이은 뒤 칸째 다듬는다
        cells = ["\n".join(_paragraph_text(p, _solid(cell._tc.find(f"{_A}tcPr"), scheme) or bg, scheme)
                           for p in cell.text_frame.paragraphs).strip()
                 for cell in row.cells]
        if any(cells):
            rows.append(" | ".join(cells))
    return rows


def _shape_hidden(shape) -> bool:
    nv = next(shape._element.iter(f"{_P}cNvPr"), None)
    return nv is not None and nv.get("hidden") in ("1", "true")


def _off_slide(shape, width: int, height: int) -> bool:
    box = (shape.left, shape.top, shape.width, shape.height)
    if None in box or not width or not height:
        return False
    left, top, w, h = (int(v) for v in box)
    return left >= width or top >= height or left + w <= 0 or top + h <= 0


def _shape_parts(shape, images: list[bytes] | None, look: dict, code: str | None = None) -> list[str]:
    """도형 하나의 글/표/그림을 반환한다. 묶음 도형은 내부 좌표순으로 재귀 처리.
    code 는 도형째 숨긴 이유(숨긴 슬라이드·숨긴 묶음·슬라이드 밖) — 그 안의 글은 모두 그 이유로 감싼다."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    code = code or ("h" if _shape_hidden(shape) else None)
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        return [
            part
            for child in sorted(shape.shapes, key=_shape_position)
            for part in _shape_parts(child, images, look, code)
        ]

    parts: list[str] = []
    if images is not None:
        from .images import mark, pptx_shape_images

        parts.extend(mark(images, blob) for blob in pptx_shape_images(shape))
    texts: list[str] = []
    if shape.has_text_frame:
        if text := _frame_text(shape.text_frame, _shape_fill(shape, look["bg"], look["scheme"]), look["scheme"]):
            texts.append(text)
    if shape.has_table:
        texts.extend(_table_rows(shape.table, look["bg"], look["scheme"]))
    parts.extend(wrap(t, code) if code else t for t in texts)
    return parts


def extract_pptx(file_bytes: bytes, images: list[bytes] | None = None) -> str:
    """images 를 주면(이미지 속 글자 검사가 켜졌을 때) 그림 도형 자리에 표시를 남기고 그림을 담는다(parser/images.py)."""
    from pptx import Presentation

    prs = Presentation(io.BytesIO(file_bytes))
    width, height = int(prs.slide_width or 0), int(prs.slide_height or 0)
    schemes: dict[int, dict] = {}

    slides: list[str] = []
    for idx, slide in enumerate(prs.slides, start=1):
        master = slide.slide_layout.slide_master
        scheme = schemes.setdefault(id(master), _scheme(master))
        look = {"scheme": scheme, "bg": _slide_bg(slide, scheme)}
        slide_code = "h" if slide._element.get("show") in ("0", "false") else None
        parts = [
            part
            for shape in sorted(slide.shapes, key=_shape_position)
            for part in _shape_parts(shape, images, look,
                                     slide_code or ("o" if _off_slide(shape, width, height) else None))
        ]
        if parts:
            slides.append(f"[슬라이드 {idx}]\n" + "\n".join(parts))

    return "\n\n---\n\n".join(slides)
