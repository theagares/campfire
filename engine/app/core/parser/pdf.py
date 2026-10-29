"""
app/core/parser/pdf.py
PDF 파싱 — pdfplumber. 파이프라인/server/pipeline/parsers/pdf.py 이식.

표 영역은 find_tables() 로 먼저 탐지하고 일반 텍스트와 표를 각각 좌표가 있는
블록으로 만든 뒤, 페이지의 위→아래·왼쪽→오른쪽 순서로 합친다. 표를 일반
텍스트에서 제외하는 것만으로는 부족하다. 일반 텍스트를 먼저 전부 append 하고
표를 나중에 append 하면 페이지 가운데 있던 표가 언제나 맨 아래로 밀린다.
스캔(이미지) PDF 는 v1 미지원 → 사전 판별해 NotImplementedError.

숨긴 글(parser/hidden.py): pdfminer 는 글자 렌더 모드를 글자에 남기지 않아 PyMuPDF get_text("rawdict") 로 찾는다 —
보이지 않음(렌더 모드 3: 채움·윤곽 둘 다 없음)·불투명도 0·실제로 그려진 크기 1pt 이하(글꼴 크기가 아니라 상자 높이 —
`1 Tf` 를 행렬로 키운 글은 크다)·쪽 밖·칠 색 = 배경(그 자리를 덮는 마지막 칠한 도형, 없으면 흰색). 그림 위의 글은 숨긴 글로
보지 않는다 — 보이지 않는 글은 스캔 문서의 OCR 글 층이고, 흰 글씨는 사진·배너 위 제목이다. 찾은 글자는 본문에서 빼고 그 쪽
끝에 표식으로 붙인다. get_texttrace 는 쓰지 않는다 — PyMuPDF 1.28 + Python 3.11 에서 None 참조 수를 깎아 문서 수십 개를 읽은
파서 프로세스가 "Fatal Python error: none_dealloc" 로 죽었다(a문서 속도 측정 말뭉치에서 재현).
PyMuPDF 상자는 회전 전·CropBox 기준이고 pdfplumber 는 회전 후·MediaBox 기준이라 옮겨서 뺀다(_to_plumber). 그래도 본문에
같은 글이 남으면 글로 한 번 더 지운다(_drop_leftover) — 숨긴 글이 본문에 그대로 새지 않게.
"""

from __future__ import annotations

import io
import logging
import re

from .hidden import color_reason, wrap

# 파서 작업자(자식 프로세스)는 로그 설정이 없어 경고가 엔진 stderr → engine.log 로 그대로 간다
logger = logging.getLogger("securedoc.engine.parser.pdf")

# 쪽당 (글 묶음 × 칠한 도형) 비교 상한 — 넘으면 그 쪽은 색 비교만 건너뛴다. 도면·차트(도형 수천 개)가 파싱 시간 상한에
# 걸려 문서가 미검사로 막히지 않게(합성 도형 5,000 + 글 2,000 쪽: 상한 없이 16.5초).
_MAX_FILL_CHECKS = 200_000


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


_FILLED, _STROKED = 16, 32  # rawdict char_flags — 둘 다 없으면 렌더 모드 3(보이지 않음)


def _span_code(span: dict, vertical: bool, page_box, images: list, fills: list | None) -> str | None:
    """rawdict 글 묶음 하나가 숨긴 글이면 이유 코드. 좌표는 모두 PyMuPDF 의 회전 전·CropBox 기준(글자·도형·그림이 같다).
    fills 가 None 이면(비용 상한) 색 비교를 건너뛴다. 예전 PyMuPDF(char_flags·alpha 없음)면 그 규칙만 건너뛴다."""
    import fitz

    box = fitz.Rect(span["bbox"])
    center = fitz.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
    on_image = any(center in img for img in images)
    flags = span.get("char_flags")
    if flags is not None and not flags & (_FILLED | _STROKED):  # 보이지 않는 글 — 그림 위면 스캔 문서의 OCR 글 층
        return None if on_image else "h"
    if span.get("alpha", 255) == 0:
        return "h"
    em = span.get("ascender", 1) - span.get("descender", 0)
    if em > 0.1 and 0 < (box.width if vertical else box.height) / em <= 1.05:  # 실제로 그려진 글자 크기
        return "s"
    if not box.intersects(page_box):
        return "o"
    if (flags is None or flags & _FILLED) and fills is not None and not on_image:  # 사진·배너 위 흰 제목은 보인다
        bg = next((fill for rect, fill in reversed(fills) if center in rect), (1, 1, 1))
        return color_reason(f"{int(span.get('color', 0)):06X}", _hex(bg))
    return None


def _to_plumber(box, page) -> tuple:
    """PyMuPDF 상자(회전 전, CropBox 왼쪽 위 기준) → pdfplumber 좌표(회전 후, MediaBox 왼쪽 위 기준)."""
    import fitz

    cb = page.cropbox
    r = fitz.Rect(box) + (cb.x0, cb.y0, cb.x0, cb.y0)
    w, h = page.mediabox.width, page.mediabox.height
    m = {90: fitz.Matrix(0, 1, -1, 0, h, 0), 180: fitz.Matrix(-1, 0, 0, -1, w, h),
         270: fitz.Matrix(0, -1, 1, 0, 0, w)}.get(page.rotation % 360)
    return tuple(r * m if m else r)


def _hidden_spans(file_bytes: bytes) -> dict[int, list[tuple[tuple, str, str]]]:
    """쪽 번호 → [(pdfplumber 좌표 상자, 글, 이유 코드)]."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return {}
    out: dict[int, list[tuple[tuple, str, str]]] = {}
    # 쪽 밖 글자도 받는다(MEDIABOX_CLIP 끄고 clip 은 무한). 그림 블록은 그림 바이트까지 담아 무거우니 빼고 상자는 get_image_info 로.
    flags = (fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_MEDIABOX_CLIP & ~fitz.TEXT_PRESERVE_IMAGES)
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            for pno, page in enumerate(doc):
                spans = [(span, abs(line["dir"][1]) > 0.7)
                         for block in page.get_text("rawdict", flags=flags, clip=fitz.INFINITE_RECT())["blocks"]
                         for line in block.get("lines", ()) for span in line["spans"]]
                if not spans:
                    continue
                page_box = fitz.Rect(0, 0, page.cropbox.width, page.cropbox.height)
                images = [fitz.Rect(i["bbox"]) for i in page.get_image_info()]
                fills: list | None = [(fitz.Rect(d["rect"]), d["fill"]) for d in page.get_drawings() if d.get("fill")]
                if len(spans) * len(fills) > _MAX_FILL_CHECKS:
                    fills = None
                for span, vertical in spans:
                    text = "".join(c["c"] for c in span.get("chars", ())).strip()
                    if text and (code := _span_code(span, vertical, page_box, images, fills)):
                        out.setdefault(pno, []).append((_to_plumber(span["bbox"], page), text, code))
    except Exception as exc:  # noqa: BLE001 - 숨긴 글을 못 찾아도 본문 추출은 지금처럼
        # 조용히 넘기면 숨긴 글 탐지가 깨져도 알 길이 없다. 문서 글은 싣지 않는다(예외 종류와 짧은 메시지만).
        logger.warning("[pdf] 숨긴 글 탐지를 건너뜀(본문 추출은 그대로): %s: %.200s", type(exc).__name__, exc)
        return {}
    return out


def _drop_leftover(body: str, texts: list[str]) -> str:
    """좌표로 빼지 못하고 본문에 남은 숨긴 글을 글로 지운다(공백 차이는 무시). 4글자 미만은 다른 글을 지울 수 있어 두지 않는다."""
    for text in texts:
        chars = [c for c in text if not c.isspace()]
        if len(chars) >= 4:
            body = re.sub(r"\s*".join(map(re.escape, chars)), "", body)
    return body


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
            leftover: list[str] = []
            if found:
                boxes = [box for box, _text, _code in found]
                kept = page.filter(lambda obj, boxes=boxes: not _in_boxes(obj, boxes))
                removed = sum(not c["text"].isspace() for c in page.chars) - sum(not c["text"].isspace() for c in kept.chars)
                if removed < sum(not ch.isspace() for _box, text, _code in found for ch in text):
                    leftover = [text for _box, text, _code in found]  # 좌표가 어긋났다 — 글로 한 번 더 지운다
                page = kept
                by_code: dict[str, list[str]] = {}
                for _box, text, code in found:
                    by_code.setdefault(code, []).append(text)
                tail = "\n" + "\n".join(wrap(" ".join(texts), code) for code, texts in by_code.items())
            table_objects = page.find_tables()
            if not table_objects:
                page_content = (_drop_leftover((page.extract_text() or "").strip(), leftover) + tail).strip()
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

            page_content = (_drop_leftover("\n".join(parts).strip(), leftover) + tail).strip()
            if page_content:
                pages.append(page_content)

    if not pages:
        raise NotImplementedError(
            "PDF 에서 텍스트를 추출할 수 없습니다. 스캔(이미지) PDF 는 지원하지 않습니다."
        )

    return "\n\n---\n\n".join(pages)
