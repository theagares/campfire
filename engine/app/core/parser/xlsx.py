"""
app/core/parser/xlsx.py
XLSX 파싱 — openpyxl (PLAN §6).

모든 시트를 순회하며 셀 텍스트를 합쳐 하나의 문서 텍스트로 반환한다
(txt.py/docx.py 와 동일하게 예외를 밖으로 던지고, 상위 parser/__init__.py 가
"미검사 통과" 정책(PLAN §9.2)에 맞춰 scan_status 로 변환한다).

숨긴 글(parser/hidden.py): 숨긴 시트(veryHidden 포함)·숨긴 행은 통째로, 숨긴 열의 칸·숫자 서식 ';;;'(값을 안 보임)·글자색 =
칸 채움색(채움 없으면 흰색)인 칸은 칸마다 표식으로 감싼다. 읽기 전용 모드는 행·열 숨김을 주지 않아 시트 XML 에서 그것만 따로
읽는다(전체 모드로 열면 큰 시트가 파싱 시간 상한을 넘는다). 테마 색은 흰색(0)·검정(1)만, 밝기 변형(tint)이 있으면 모르는 색.
"""

from __future__ import annotations

import io

from .hidden import color_reason, wrap


def _hidden_rows_cols(ws) -> tuple[set[int], list[tuple[int, int]]]:
    """시트 XML 의 <row hidden="1">·<col hidden="1"> — (숨긴 행 번호, 숨긴 열 범위)."""
    from openpyxl.xml.functions import iterparse  # defusedxml 이 있으면 그걸 쓴다

    rows: set[int] = set()
    cols: list[tuple[int, int]] = []
    last = 0
    with ws.parent._archive.open(ws._worksheet_path) as f:
        for _event, el in iterparse(f):
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "col" and el.get("hidden") in ("1", "true"):
                cols.append((int(el.get("min", 0)), int(el.get("max", 0))))
            elif tag == "row":
                last = int(el.get("r") or last + 1)
                if el.get("hidden") in ("1", "true"):
                    rows.add(last)
                el.clear()  # 큰 시트도 칸을 쌓아 두지 않는다
    return rows, cols


def _rgb(color) -> str | None:
    if color is None:
        return None
    if color.type == "rgb":
        return color.rgb if isinstance(color.rgb, str) else None
    if color.type == "theme" and not color.tint:
        return {0: "FFFFFF", 1: "000000"}.get(color.theme)
    if color.type == "indexed" and isinstance(color.indexed, int) and 0 <= color.indexed < 64:
        from openpyxl.styles.colors import COLOR_INDEX

        return COLOR_INDEX[color.indexed]
    return None


def _cell_code(cell, hidden_col: bool) -> str | None:
    if hidden_col or set(str(getattr(cell, "number_format", "") or "")) == {";"}:
        return "h"
    font = getattr(cell, "font", None)
    fg = _rgb(font.color) if font is not None else None
    if fg is None:
        return None
    fill = cell.fill
    bg = "FFFFFF" if fill is None or fill.fill_type is None else _rgb(fill.fgColor) if fill.fill_type == "solid" else None
    return color_reason(fg, bg)


def extract_xlsx(file_bytes: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    try:
        sheets: list[str] = []
        for ws in wb.worksheets:
            hidden_rows, hidden_cols = _hidden_rows_cols(ws)
            rows: list[str] = []
            for row in ws.iter_rows():
                if not any(cell.value is not None for cell in row):
                    continue
                row_no = next(cell.row for cell in row if cell.value is not None)
                cells = []
                for cell in row:
                    value = str(cell.value).strip() if cell.value is not None else ""
                    code = _cell_code(cell, any(a <= cell.column <= b for a, b in hidden_cols)) \
                        if value and row_no not in hidden_rows else None
                    cells.append(wrap(value, code) if code else value)
                populated = [idx for idx, cell in enumerate(cells) if cell]
                if populated:
                    # 오른쪽의 의미 없는 빈 칸은 버리되, 앞/중간의 빈 칸은 남긴다.
                    # 예: A와 C에 값이 있으면 "A |  | C"여야 C가 B로 당겨지지 않는다.
                    line = " | ".join(cells[: populated[-1] + 1])
                    rows.append(wrap(line, "h") if row_no in hidden_rows else line)
            if rows:
                body = "\n".join(rows)
                hidden_sheet = getattr(ws, "sheet_state", "visible") in ("hidden", "veryHidden")
                sheets.append(f"[{ws.title}]\n" + (wrap(body, "h") if hidden_sheet else body))
        return "\n\n---\n\n".join(sheets)
    finally:
        wb.close()
