"""
app/core/masker/docwrapper.py
마스킹된 텍스트를 업로드할 파일로 감싼다 — 늘 <이름>_masked.md(text/plain), 확장 utils/docwrapper.js wrapAsMd 와 같다.

예전엔 PLAN §6 대로 DOCX 로 감쌌다. 그런데 확장·프록시는 늘 .md 라, 확장 방식에서 패널에서 아무것도 안 풀면 엔진 DOCX,
하나라도 풀면 확장 MD 로 같은 파일이 두 형식으로 나갔고, 붙여 넣은 글(text/plain)도 upload_masked.docx 로 바뀌어
올라갔다(맥 v0.2.60 M-8). 파서가 레이아웃을 이미 버려 DOCX 로 되살릴 것도 없다 — 늘 .md 로 맞춘다(사용자 결정).
"""

from __future__ import annotations

MD_MIME = "text/plain"  # 확장 wrapAsMd 와 같다 — 패널 두 경로(엔진 maskedFile·확장 감싸기)가 같은 파일이 된다


def _masked_name(orig_file_name: str, new_ext: str) -> str:
    """원본 파일명 → *_masked.<new_ext>. (docwrapper.js 파일명 규칙 이식)"""
    if "." in orig_file_name:
        base = orig_file_name.rsplit(".", 1)[0]
    else:
        base = orig_file_name or "masked_document"
    new_ext = new_ext.lstrip(".")
    return f"{base}_masked.{new_ext}"


def wrap_masked_file(masked_text: str, orig_file_name: str) -> dict:
    """마스킹 텍스트를 .md 파일로 감싼다. 반환 {bytes, mime_type, file_name}.

    짝 없는 서로게이트는 UTF-8 로 쓸 수 없어 바꿔 넣는다 — 예전 DOCX 의 XML 오류처럼 /jobs 가 500 이 되지 않게.
    """
    return {
        "bytes": masked_text.encode("utf-8", "replace"),
        "mime_type": MD_MIME,
        "file_name": _masked_name(orig_file_name, "md"),
    }
