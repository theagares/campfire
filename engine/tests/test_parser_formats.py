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
