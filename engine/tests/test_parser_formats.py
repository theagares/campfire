"""추가 포맷 파서 테스트 — ODF / EML / HTML (전부 표준 라이브러리 기반).

pytest 없이도 돌도록 __main__ 진입점을 둔다:  python engine/tests/test_parser_formats.py
"""
from __future__ import annotations

import io
import zipfile

from app.core.parser.eml import extract_eml
from app.core.parser.html import extract_html
from app.core.parser.odf import extract_odf

_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
)


def _odt(body: str, styles: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("content.xml", f'<?xml version="1.0"?><office:document-content {_NS}>'
                                  f"<office:body><office:text>{body}</office:text></office:body>"
                                  "</office:document-content>")
        if styles:
            z.writestr("styles.xml", f'<?xml version="1.0"?><office:document-styles {_NS}>'
                                     f"<office:master-styles>{styles}</office:master-styles>"
                                     "</office:document-styles>")
    return buf.getvalue()


def test_odf_keeps_inline_span_and_splits_cells():
    out = extract_odf(_odt(
        "<text:p>홍길동 <text:span>010-1234-5678</text:span></text:p>"
        "<table:table><table:table-row>"
        "<table:table-cell><text:p>김철수</text:p></table:table-cell>"
        "<table:table-cell><text:p>kim@corp.co.kr</text:p></table:table-cell>"
        "</table:table-row></table:table>",
        "<text:p>대외비</text:p>",
    ))
    # 인라인 서식은 이어 붙어야 전화번호가 한 값으로 잡힌다
    assert "홍길동 010-1234-5678" in out
    # 셀은 끊어야 이름과 이메일이 한 값으로 뭉치지 않는다
    assert "김철수 | kim@corp.co.kr" in out
    # 머리말/꼬리말(styles.xml)에 "대외비" 도장이 자주 있다
    assert "대외비" in out


def test_odf_survives_missing_styles():
    assert extract_odf(_odt("<text:p>정상</text:p>")) == "정상"


def _eml() -> bytes:
    head = (
        "From: =?utf-8?B?7ZmN6ri464+Z?= <hong@corp.co.kr>\r\n"
        "To: kim@partner.com\r\n"
        "Subject: =?utf-8?B?6rOE7JW9?=\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        "--B\r\n"
        'Content-Type: text/plain; charset="euc-kr"\r\n\r\n'
    ).encode()
    tail = (
        "\r\n--B\r\n"
        'Content-Disposition: attachment; filename="=?utf-8?B?6rOE7JW97IScLnBkZg==?="\r\n\r\n'
        "X\r\n--B--\r\n"
    ).encode()
    return head + "단가 12,400원".encode("euc-kr") + tail


def test_eml_headers_body_and_attachment_name():
    out = extract_eml(_eml())
    assert "hong@corp.co.kr" in out and "kim@partner.com" in out   # 헤더가 PII 밀도 최상
    assert "홍길동" in out                                          # MIME 인코딩 헤더
    assert "12,400" in out                                         # euc-kr 본문
    assert "계약서.pdf" in out                                      # 첨부는 이름만


def test_html_strips_script_and_keeps_inline_text():
    html = (
        '<html><head><style>.a{color:red}</style>'
        '<script>var pw="secret123"</script></head>'
        "<body><p>담당자 <b>박</b>영희</p>"
        "<table><tr><td>사번</td><td>A-1024</td></tr></table></body></html>"
    )
    out = extract_html(html.encode())
    assert "secret123" not in out and "color:red" not in out   # 탐지기 입력 오염 방지
    assert "박영희" in out                                      # 인라인 태그가 이름을 끊으면 미탐
    assert "사번" in out and "A-1024" in out


def test_html_survives_unclosed_tags():
    assert "살아있음" in extract_html("<p>살아있음<div>".encode())


def test_html_never_swallows_document_on_unclosed_script():
    """닫히지 않은 script/style/head 가 문서를 통째로 삼키면 안 된다.

    파서 안에서 태그 깊이를 세어 건너뛰던 구현은 여기서 빈 문자열을 냈다. 그러면
    STATUS_OK + 탐지 0건으로 끝나 "검사했다" 고 표시되지만 실제로는 아무것도 안 본다.
    """
    for broken in (
        "<script>var a=1;<p>홍길동 010-1234-5678</p>",
        "<style>.a{}<p>홍길동 010-1234-5678</p>",
        "<head><title>x</title><p>홍길동 010-1234-5678</p>",
    ):
        out = extract_html(broken.encode())
        assert "홍길동" in out, broken
        assert "010-1234-5678" in out, broken


def test_html_keeps_visible_text_around_literal_script_tokens():
    """주석·속성값·textarea 안의 리터럴 <script> 는 여는 태그가 아니다.

    정규식으로 script 를 걷어내던 구현은 이것들을 여는 태그로 보고 다음 진짜
    </script> 까지 lazy 로 삼켜, 그 사이 보이는 텍스트(이름·전화)가 통째로 사라졌다.
    스캐너는 빈 문자열을 보고 STATUS_OK·탐지 0건으로 끝난다 — 침묵 검사 우회.
    """
    pii = "홍길동 010-1234-5678"
    for label, html in (
        ("주석", f"<!-- <script> --> {pii} <script>x</script>"),
        ("속성값", f'<div title="<script>">{pii}</div><script>x</script>'),
        ("textarea", f"<textarea><script></textarea>{pii}<script>x</script>"),
    ):
        out = extract_html(html.encode())
        assert "홍길동" in out and "010-1234-5678" in out, label


def test_html_parse_is_linear_not_quadratic():
    """안 닫힌 <script opener 가 많아도 O(n²) 로 안 터진다.

    정규식 시절엔 opener 마다 EOF 까지 다시 훑어 ~1MB 에서 30초를 넘겼고, 그러면
    파싱 타임아웃 → 미검사 통과가 됐다. 파서 방식은 선형이라 1초도 안 걸린다.
    """
    import time

    blob = ("<script " * (1024 * 1024 // 8)).encode()  # ~1MB, 전부 안 닫힌 opener
    start = time.perf_counter()
    extract_html(blob)
    assert time.perf_counter() - start < 5.0


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for fn in fns:
        try:
            fn()
            print("  OK   " + fn.__name__)
        except AssertionError as exc:
            bad += 1
            print("  FAIL " + fn.__name__, exc)
    print(f"{len(fns) - bad}/{len(fns)} 통과")
    sys.exit(1 if bad else 0)
