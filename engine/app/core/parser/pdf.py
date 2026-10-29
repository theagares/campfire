"""
app/core/parser/pdf.py
PDF 파싱 — pdfplumber. 파이프라인/server/pipeline/parsers/pdf.py 이식.

표 영역은 find_tables() 로 먼저 탐지하고 일반 텍스트와 표를 각각 좌표가 있는
블록으로 만든 뒤, 페이지의 위→아래·왼쪽→오른쪽 순서로 합친다. 표를 일반
텍스트에서 제외하는 것만으로는 부족하다. 일반 텍스트를 먼저 전부 append 하고
표를 나중에 append 하면 페이지 가운데 있던 표가 언제나 맨 아래로 밀린다.
스캔(이미지) PDF 는 v1 미지원 → 사전 판별해 NotImplementedError.

숨긴 글(parser/hidden.py): pdfminer 는 글자 렌더 모드를 글자에 남기지 않아 PyMuPDF 글자 추적(get_texttrace)으로 찾는다 —
렌더 모드 3(보이지 않음)·불투명도 0·1pt 이하·쪽 밖·칠 색 = 배경(그 아래 마지막으로 칠한 도형, 없으면 흰색). 그림 위의 보이지
않는 글은 스캔 문서의 OCR 글 층이라 뺀다. 찾은 글자는 본문에서 빼고 그 쪽 끝에 표식으로 붙인다(숨긴 글이라 자리는 중요하지 않다).
"""

from __future__ import annotations

import io

from .hidden import color_reason, wrap


def _hex(color, components: int = 3) -> str | None:
    """PyMuPDF 색(0~1 실수: 회색 1개·RGB 3개·CMYK 4개) → RGB 16진."""
    c = tuple(color or ())
    if len(c) == 1:
        c = c * 3
    elif len(c) == 4:
        k = c[3]
        c = tuple((1 - x) * (1 - k) for x in c[:3])
    if len(c) != 3:
        return None
    return "".join(f"{round(max(0.0, min(1.0, float(x))) * 255):02X}" for x in c)


def _span_code(span: dict, page_rect, images: list, fills: list) -> str | None:
    import fitz

    box = fitz.Rect(span["bbox"])
    center = fitz.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
    if span.get("type") == 3:  # 보이지 않는 글 — 그림 위면 스캔 문서의 OCR 글 층
        return None if any(center in img for img in images) else "h"
    if span.get("opacity", 1) == 0:
        return "h"
    if 0 < span.get("size", 12) <= 1:
        return "s"
    if not box.intersects(page_rect):
        return "o"
    if span.get("type") in (0, 2):
        bg = next((fill for rect, fill, seq in reversed(fills) if seq < span.get("seqno", 0) and center in rect), (1, 1, 1))
        return color_reason(_hex(span.get("color")), _hex(bg))
    return None


def _hidden_spans(file_bytes: bytes) -> dict[int, list[tuple[tuple, str, str]]]:
    """쪽 번호 → [(글자 상자, 글, 이유 코드)]."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return {}
    out: dict[int, list[tuple[tuple, str, str]]] = {}
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            for pno, page in enumerate(doc):
                images = [fitz.Rect(i["bbox"]) for i in page.get_image_info()]
                fills = [(fitz.Rect(d["rect"]), d["fill"], d.get("seqno", 0)) for d in page.get_drawings() if d.get("fill")]
                for span in page.get_texttrace():
                    text = "".join(chr(c[0]) for c in span.get("chars", ()) if c[0] > 0).strip()
                    if text and (code := _span_code(span, page.rect, images, fills)):
                        out.setdefault(pno, []).append((tuple(span["bbox"]), text, code))
    except Exception:  # noqa: BLE001 - 숨긴 글을 못 찾아도 본문 추출은 지금처럼
        return {}
    return out


def _in_boxes(obj: dict, boxes: list[tuple]) -> bool:
    if obj.get("object_type") != "char":
        return False
    cx = (float(obj.get("x0", 0)) + float(obj.get("x1", 0))) / 2
    cy = (float(obj.get("top", 0)) + float(obj.get("bottom", 0))) / 2
    return any(x0 - 0.5 <= cx <= x1 + 0.5 and y0 - 0.5 <= cy <= y1 + 0.5 for x0, y0, x1, y1 in boxes)


def _table_text(table_obj) -> str:
    """표를 행 단위 텍스트로 바꾸되 빈 칸의 열 위치는 유지한다."""
    rows: list[str] = []
    for row in table_obj.extract() or []:
        cells = [str(cell).strip() if cell is not None else "" for cell in row]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _inside_table(obj: dict, bboxes: list[tuple[float, float, float, float]]) -> bool:
    """문자 중심이 표 안이면 일반 텍스트 추출에서 제외한다.

    글리프가 표 경계선에 살짝 걸치는 흔한 PDF 에서 사각형 전체가 표 안에 있어야
    한다고 요구하면 같은 글자가 본문과 표 양쪽에 중복된다.
    """
    center_x = (float(obj.get("x0", 0)) + float(obj.get("x1", 0))) / 2
    center_y = (float(obj.get("top", 0)) + float(obj.get("bottom", 0))) / 2
    return any(
        x0 <= center_x <= x1 and top <= center_y <= bottom
        for x0, top, x1, bottom in bboxes
    )


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

    hidden = _hidden_spans(file_bytes)
    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for pno, page in enumerate(pdf.pages):
            found = hidden.get(pno, [])
            tail = ""
            if found:
                boxes = [box for box, _text, _code in found]
                page = page.filter(lambda obj, boxes=boxes: not _in_boxes(obj, boxes))
                by_code: dict[str, list[str]] = {}
                for _box, text, code in found:
                    by_code.setdefault(code, []).append(text)
                tail = "\n" + "\n".join(wrap(" ".join(texts), code) for code, texts in by_code.items())
            table_objects = page.find_tables()
            if not table_objects:
                page_content = ((page.extract_text() or "").strip() + tail).strip()
                if page_content:
                    pages.append(page_content)
                continue

            # (top, left, original_order, text). 추출에 실패한 가짜 표는 bbox 로 본문을
            # 지우지 않는다. 표선처럼 보이는 장식 때문에 내용이 사라질 수 있어서다.
            blocks: list[tuple[float, float, int, str]] = []
            real_tables = []
            for order, table_obj in enumerate(table_objects):
                text = _table_text(table_obj)
                if not text:
                    continue
                real_tables.append(table_obj)
                x0, top, _x1, _bottom = table_obj.bbox
                blocks.append((float(top), float(x0), order, text))

            table_bboxes = [table.bbox for table in real_tables]
            text_page = page.filter(lambda obj: not _inside_table(obj, table_bboxes))
            text_order_start = len(blocks)
            for order, line in enumerate(text_page.extract_text_lines(return_chars=False)):
                text = str(line.get("text", "")).strip()
                if text:
                    blocks.append(
                        (float(line["top"]), float(line["x0"]), text_order_start + order, text)
                    )

            # 같은 높이에 있는 블록은 왼쪽부터 읽는다. original_order 는 좌표가
            # 완전히 같은 비정상 PDF 에서도 결과가 실행마다 바뀌지 않게 한다.
            blocks.sort(key=lambda block: (block[0], block[1], block[2]))
            parts = [block[3] for block in blocks]

            page_content = ("\n".join(parts).strip() + tail).strip()
            if page_content:
                pages.append(page_content)

    if not pages:
        raise NotImplementedError(
            "PDF 에서 텍스트를 추출할 수 없습니다. 스캔(이미지) PDF 는 지원하지 않습니다."
        )

    return "\n\n---\n\n".join(pages)
