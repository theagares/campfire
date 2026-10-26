"""
app/core/parser/xlsx.py
XLSX 파싱 — openpyxl (PLAN §6).

모든 시트를 순회하며 셀 텍스트를 합쳐 하나의 문서 텍스트로 반환한다
(txt.py/docx.py 와 동일하게 예외를 밖으로 던지고, 상위 parser/__init__.py 가
"미검사 통과" 정책(PLAN §9.2)에 맞춰 scan_status 로 변환한다).
"""

from __future__ import annotations

import io


def extract_xlsx(file_bytes: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    try:
        sheets: list[str] = []
        for ws in wb.worksheets:
            rows: list[str] = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(cell).strip() if cell is not None else "" for cell in row]
                populated = [idx for idx, cell in enumerate(cells) if cell]
                if populated:
                    # 오른쪽의 의미 없는 빈 칸은 버리되, 앞/중간의 빈 칸은 남긴다.
                    # 예: A와 C에 값이 있으면 "A |  | C"여야 C가 B로 당겨지지 않는다.
                    rows.append(" | ".join(cells[: populated[-1] + 1]))
            if rows:
                sheets.append(f"[{ws.title}]\n" + "\n".join(rows))
        return "\n\n---\n\n".join(sheets)
    finally:
        wb.close()
