"""가린 글은 늘 <이름>_masked.md(text/plain)로 감싼다 — 확장 utils/docwrapper.js wrapAsMd 와 같은 이름·형식(사용자 결정).

예전엔 DOCX 로 감싸 확장 방식에서 같은 파일이 DOCX(아무것도 안 풂)·MD(하나라도 풂) 두 형식으로 나갔고, 제어 문자가 섞인 글은
XML 오류로 /jobs 가 500 이 됐다(맥 v0.2.60 M-8).
"""

from __future__ import annotations

from app.core.masker import docwrapper


def test_늘_masked_md_text_plain_으로_감싼다():
    for orig, want in (("보고서.pdf", "보고서_masked.md"), ("pii.docx", "pii_masked.md"), ("upload", "upload_masked.md"),
                       ("a.b.txt", "a.b_masked.md"), ("", "masked_document_masked.md")):
        out = docwrapper.wrap_masked_file("첫 줄\n[이름 마스킹]", orig)
        assert (out["file_name"], out["mime_type"]) == (want, "text/plain"), orig
        assert out["bytes"] == "첫 줄\n[이름 마스킹]".encode()


def test_제어_문자_서로게이트가_섞여도_감싼다():
    out = docwrapper.wrap_masked_file("담당\x00\x0b 연락처\x0c\n끝\ud800 😀", "upload")
    assert out["bytes"] == "담당\x00\x0b 연락처\x0c\n끝? 😀".encode()
