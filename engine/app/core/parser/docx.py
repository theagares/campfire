"""
app/core/parser/docx.py
DOCX 파싱 — python-docx. 파이프라인/server/pipeline/parsers/docx.py 이식.

문단과 표를 문서에 나온 순서대로 꺼낸다. 예전엔 문단을 다 꺼낸 뒤 표를 맨 끝에 몰아 붙여, 표가 본문 어디에
있었는지 사라졌다(앞뒤 문단과 떨어져 기밀 판정·AI 답이 맥락을 잃었다).

숨긴 글(parser/hidden.py): 숨김(w:vanish·w:specVanish), 1pt 이하(w:sz ≤ 2), 글자색 = 실제 배경색(글자 형광·음영 → 문단 음영 →
칸·표 음영 → 쪽 배경 → 흰색 순으로 본 결과)인 글자를 표식으로 감싼다. 글자 속성은 직접 서식 → 글자 스타일 → 문단 스타일
(basedOn 사슬) → 문서 기본값 순으로 찾는다. 표 스타일·테마 색 변형은 보지 않는다(모르는 색은 숨기지 않은 것으로).
"""

from __future__ import annotations

import io

from .hidden import color_reason, wrap

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HIGHLIGHT = {
    "black": "000000", "blue": "0000FF", "cyan": "00FFFF", "green": "00FF00", "magenta": "FF00FF", "red": "FF0000",
    "yellow": "FFFF00", "white": "FFFFFF", "darkBlue": "000080", "darkCyan": "008080", "darkGreen": "008000",
    "darkMagenta": "800080", "darkRed": "800000", "darkYellow": "808000", "darkGray": "808080", "lightGray": "C0C0C0",
}


class _Look:
    """숨긴 글을 가리는 데 필요한 문서 전체 정보(스타일·쪽 배경)."""

    def __init__(self, doc) -> None:
        styles = doc.styles.element
        self.styles = {s.get(f"{_W}styleId"): s for s in styles.iter(f"{_W}style")}
        default = next((s for s in styles.iter(f"{_W}style")
                        if s.get(f"{_W}type") == "paragraph" and s.get(f"{_W}default") in ("1", "true")), None)
        self.default_pstyle = default.get(f"{_W}styleId") if default is not None else None
        defaults = styles.find(f"{_W}docDefaults/{_W}rPrDefault/{_W}rPr")
        self.defaults = [defaults] if defaults is not None else []
        bg = doc.element.find(f"{_W}background")
        self.page_bg = bg.get(f"{_W}color") if bg is not None and bg.get(f"{_W}color") not in (None, "auto") else "FFFFFF"

    def _chain(self, style_id: str | None) -> list:
        out, seen = [], set()
        while style_id and style_id not in seen and (st := self.styles.get(style_id)) is not None:
            seen.add(style_id)
            if (rpr := st.find(f"{_W}rPr")) is not None:
                out.append(rpr)
            based = st.find(f"{_W}basedOn")
            style_id = based.get(f"{_W}val") if based is not None else None
        return out

    def rprs(self, r, p_el) -> list:
        """글자 속성이 놓일 수 있는 rPr 들, 가까운 것부터."""
        rpr = r.find(f"{_W}rPr")
        rstyle = rpr.find(f"{_W}rStyle") if rpr is not None else None
        pstyle = p_el.find(f"{_W}pPr/{_W}pStyle")
        return ([rpr] if rpr is not None else []) \
            + self._chain(rstyle.get(f"{_W}val") if rstyle is not None else None) \
            + self._chain(pstyle.get(f"{_W}val") if pstyle is not None else self.default_pstyle) + self.defaults


def _prop(rprs: list, tag: str):
    return next((el for rpr in rprs if (el := rpr.find(f"{_W}{tag}")) is not None), None)


def _on(el) -> bool:
    return el is not None and el.get(f"{_W}val", "true") not in ("false", "0", "off")


def _shade(el) -> str | None:
    """w:shd 의 실제 칠 색. 무늬 없이 칠만(clear) 이면 fill, 꽉 찬 무늬(solid)면 color."""
    if el is None or el.get(f"{_W}val") == "nil":
        return None
    color = el.get(f"{_W}color") if el.get(f"{_W}val") == "solid" else el.get(f"{_W}fill")
    return color if color and color != "auto" else None


def _background(rprs: list, p_el, look: _Look) -> str:
    hl = _prop(rprs, "highlight")
    if hl is not None and hl.get(f"{_W}val") in _HIGHLIGHT:
        return _HIGHLIGHT[hl.get(f"{_W}val")]
    for el in ([_prop(rprs, "shd"), p_el.find(f"{_W}pPr/{_W}shd")]
               + p_el.xpath("ancestor::w:tc[1]/w:tcPr/w:shd | ancestor::w:tbl[1]/w:tblPr/w:shd")[::-1]):
        if (color := _shade(el)) is not None:
            return color
    return look.page_bg


def _hidden_code(r, p_el, look: _Look) -> str | None:
    rprs = look.rprs(r, p_el)
    if _on(_prop(rprs, "vanish")) or _on(_prop(rprs, "specVanish")):
        return "h"
    sz = _prop(rprs, "sz")
    if sz is not None and (sz.get(f"{_W}val") or "").isdigit() and int(sz.get(f"{_W}val")) <= 2:
        return "s"
    color = _prop(rprs, "color")
    if color is not None and color.get(f"{_W}val") not in (None, "auto"):
        return color_reason(color.get(f"{_W}val"), _background(rprs, p_el, look))
    return None


def _visible_text(p, look: _Look | None) -> str:
    """p.text 와 같은 글(w:r | w:hyperlink 의 글자) — 숨긴 글자만 표식으로 감싼다."""
    if look is None:
        return p.text
    parts, hidden = [], False
    for el in p._p.xpath("w:r | w:hyperlink"):
        for r in ([el] if el.tag == f"{_W}r" else el.xpath("w:r")):
            code = _hidden_code(r, p._p, look) if r.text else None
            hidden = hidden or code is not None
            parts.append(wrap(r.text, code) if code else r.text)
    return "".join(parts) if hidden else p.text


def extract_docx(file_bytes: bytes, images: list[bytes] | None = None) -> str:
    """images 를 주면(이미지 속 글자 검사가 켜졌을 때) 그림이 든 문단(표 칸 안 포함) 자리에 표시를 남기고 그림을 담는다
    (parser/images.py)."""
    from docx import Document
    from docx.table import Table

    doc = Document(io.BytesIO(file_bytes))
    look = _Look(doc)
    blocks: list[str] = []
    for item in doc.iter_inner_content():
        if isinstance(item, Table):
            blocks += _table_rows(item, images, look)
        elif text := _paragraph_text(item, images, look):
            blocks.append(text)
    return "\n\n".join(blocks)


def _paragraph_text(p, images: list[bytes] | None, look: _Look | None = None) -> str:
    text = _visible_text(p, look) if p.text.strip() else ""
    if images is not None:
        from .images import docx_paragraph_images, mark

        marks = "".join(mark(images, blob) for blob in docx_paragraph_images(p))
        text = f"{text}\n{marks}".strip("\n") if marks else text
    return text


def _table_rows(table, images: list[bytes] | None, look: _Look | None = None) -> list[str]:
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
                    parts.extend(_table_rows(item, images, look))
                elif text := _paragraph_text(item, images, look).strip():
                    parts.append(text)
            cells.append("\n".join(parts))
        if any(cells):
            rows.append(" | ".join(cells))
    return rows
