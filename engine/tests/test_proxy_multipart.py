"""multipart 재작성 — 파트 헤더를 잃지 않는지가 핵심이다.

mitmproxy 없이 도는 테스트다. 프록시 경로에서 실제로 틀리기 쉬운 곳은
TLS 나 애드온 배선이 아니라 "본문을 갈아끼우면서 filename/Content-Type 을
제대로 바꿨는가" 라서, 그 부분만 따로 떼어 검증한다.
"""

from __future__ import annotations

import pytest

from app.adapters.proxy.multipart import MultipartError, decode, encode

CT = 'multipart/form-data; boundary=----WebKitFormBoundaryABC123'


def _body(*parts: bytes) -> bytes:
    sep = b"------WebKitFormBoundaryABC123"
    out = bytearray()
    for p in parts:
        out += sep + b"\r\n" + p + b"\r\n"
    out += sep + b"--\r\n"
    return bytes(out)


FILE_PART = (
    b'Content-Disposition: form-data; name="file"; filename="\xec\x84\xa4\xea\xb3\x84.docx"\r\n'
    b"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    b"\r\n\r\nPK\x03\x04binary-ish"
)
TEXT_PART = b'Content-Disposition: form-data; name="conversation_id"\r\n\r\nabc-123'


def test_파일_파트와_일반_필드를_구분한다():
    parts = decode(_body(TEXT_PART, FILE_PART), CT)
    assert len(parts) == 2
    assert [p.is_file() for p in parts] == [False, True]
    assert parts[1].filename == "설계.docx"
    assert "wordprocessingml" in parts[1].content_type


def test_filename_빈_문자열은_파일이_아니다():
    """<input type=file> 을 안 고르면 filename="" 인 파트가 그대로 온다.

    이걸 파일로 세면 0바이트를 검사하게 되고, 사용자는 왜 빈 문서가 걸렸는지
    알 수 없다.
    """
    empty = b'Content-Disposition: form-data; name="file"; filename=""\r\n\r\n'
    parts = decode(_body(empty), CT)
    assert parts[0].is_file() is False


def test_교체하면_이름과_타입이_함께_바뀐다():
    """`.docx` 로 적힌 채 MD 바이트를 보내면 받는 쪽이 DOCX 로 열려다 실패한다."""
    parts = decode(_body(TEXT_PART, FILE_PART), CT)
    parts[1] = parts[1].replaced(
        body=b"# masked\n", filename="설계_masked.md", content_type="text/markdown"
    )
    rebuilt = encode(parts, CT)

    again = decode(rebuilt, CT)
    assert again[1].filename == "설계_masked.md"
    assert again[1].content_type == "text/markdown"
    assert again[1].body == b"# masked\n"
    # 건드리지 않은 파트는 그대로여야 한다 — 재작성이 다른 필드를 흘리면
    # 사이트가 요청 자체를 거부한다.
    assert again[0].body == b"abc-123"
    assert again[0].filename is None


def test_Content_Type_없는_파일_파트에도_타입을_붙인다():
    no_ct = b'Content-Disposition: form-data; name="f"; filename="a.txt"\r\n\r\nhello'
    parts = decode(_body(no_ct), CT)
    out = parts[0].replaced(body=b"x", filename="a_masked.md", content_type="text/markdown")
    assert b"text/markdown" in out.headers


def test_boundary_없으면_예외():
    """해석 못 한 본문을 통과시키면 검사 없이 나간다. 예외로 올려 차단시킨다."""
    with pytest.raises(MultipartError):
        decode(b"whatever", "multipart/form-data")
