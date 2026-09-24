"""파일 종류를 바이트로 알아내기.

프록시 경로에서는 이름도 타입도 못 믿는다(2026-09-21 실측):
  - ChatGPT upload_reservations 등록에는 file_name 이 없다.
  - Gemini 도 이름이 없고, Content-Type 을 x-www-form-urlencoded 라고 준다.

이걸 그대로 엔진에 넘기면 PDF·DOCX 가 "미지원" 으로 떨어진다. 즉 **검사되지 않는다.**
아래 마지막 테스트가 그 인과를 직접 확인한다 — 고치기 전에는 정말로 파싱이 실패한다.
"""

from __future__ import annotations

import io
import zipfile

from app.adapters.proxy.sniff import refine, sniff

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
LYING_MIME = "application/x-www-form-urlencoded"  # Gemini 가 실제로 보내는 값


def _docx_bytes() -> bytes:
    """python-docx 로 진짜 DOCX 를 만든다(엔진 의존성이라 이미 있다)."""
    import docx

    d = docx.Document()
    d.add_paragraph("홍길동 010-1234-5678")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _zip_with(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in entries.items():
            z.writestr(name, body)
    return buf.getvalue()


def test_PDF_를_알아본다():
    assert sniff(b"%PDF-1.7\n1 0 obj\n") == (".pdf", "application/pdf")


def test_진짜_DOCX_를_알아본다():
    assert sniff(_docx_bytes()) == (".docx", DOCX_MIME)


def test_ODF_는_mimetype_항목으로_가린다():
    odt = _zip_with({"mimetype": b"application/vnd.oasis.opendocument.text",
                     "content.xml": b"<x/>"})
    assert sniff(odt) == (".odt", "application/vnd.oasis.opendocument.text")


def test_평문은_txt_로_본다():
    assert sniff("주민등록번호: 900101-1234567\n".encode()) == (".txt", "text/plain")


def test_모르는_바이너리는_None():
    """못 알아본 것을 아무거나로 찍으면 엉뚱한 파서로 보내게 된다.

    모르면 모른다고 두고 엔진이 '미지원' 으로 거절하게 한다 — 거절은 유출이 아니다.
    """
    assert sniff(b"\x00\x01\x02\x03binary junk") is None
    assert sniff(b"") is None


def test_깨진_ZIP_은_None():
    assert sniff(b"PK\x03\x04" + b"\xff" * 50) is None


def test_쓸만한_확장자가_있으면_건드리지_않는다():
    """사용자가 보는 이름이다. 알아맞힐 수 있어도 바꾸지 않는다."""
    name, mime = refine(_docx_bytes(), "계약서.docx", DOCX_MIME)
    assert name == "계약서.docx" and mime == DOCX_MIME


def test_이름이_껍데기면_바이트로_채운다():
    name, mime = refine(_docx_bytes(), "upload.bin", LYING_MIME)
    assert name == "upload.docx"
    assert mime == DOCX_MIME


def test_확장자가_아예_없어도_채운다():
    name, mime = refine(b"%PDF-1.7\n", "upload", "")
    assert name == "upload.pdf" and mime == "application/pdf"


# ── 이 수정이 실제로 필요한 이유 ────────────────────────────────────────────


def test_고치기_전에는_엔진이_DOCX_를_못_읽는다():
    """인과를 직접 확인한다. 모델은 안 쓰고 파서만 부른다."""
    from app.core.parser import parse_document

    data = _docx_bytes()

    # 사이트가 준 그대로 넘기면 — 이름은 upload.bin, 타입은 거짓말.
    _text, status, _reason = parse_document(data, LYING_MIME, "upload.bin")
    assert status != "ok", "고치기 전에도 읽혔다면 이 수정은 의미가 없다"

    # 바이트를 보고 채운 뒤에는 읽힌다.
    name, mime = refine(data, "upload.bin", LYING_MIME)
    text, status2, _ = parse_document(data, mime, name)
    assert status2 == "ok"
    assert "홍길동" in text
