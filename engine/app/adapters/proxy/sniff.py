"""
app/adapters/proxy/sniff.py
바이트를 보고 파일 종류를 알아낸다.

프록시 경로에서는 파일 이름을 못 받는 경우가 흔하다(2026-09-21 실측):
  - ChatGPT 의 upload_reservations 등록 요청에는 file_name 이 없다.
  - Gemini 의 resumable 업로드도 이름을 안 싣는다. 게다가 Content-Type 이
    application/x-www-form-urlencoded 라고 적혀 오는데 **본문은 파일 바이트다** —
    사이트가 말해 주는 타입이 오히려 거짓이다.

엔진 파서는 확장자 **또는** MIME 중 하나만 맞으면 동작한다(core/parser/__init__).
그래서 둘 다 못 믿을 때는 여기서 바이트를 보고 채워 준다. 매직 바이트는 거짓말을
하지 않으므로, 알아본 경우에는 사이트가 준 값보다 이쪽을 믿는다.

ponytail: OLE 계열(\\xd0\\xcf\\x11\\xe0)은 다루지 않는다. HWP·구버전 doc/xls 가
같은 매직을 쓰는데, 잘못 찍으면 .doc 을 HWP 파서로 보내게 된다. 못 알아본 것은
그대로 두고 엔진이 "미지원" 으로 처리하게 둔다 — 그건 유출이 아니라 거절이다.
"""

from __future__ import annotations

import codecs
import io
import zipfile

# ZIP 컨테이너 안에 무엇이 들어 있으면 어떤 포맷인지.
# 순서가 의미 있다 — 먼저 맞는 것을 쓴다.
_ZIP_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("word/document.xml", ".docx",
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("xl/workbook.xml", ".xlsx",
     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("ppt/presentation.xml", ".pptx",
     "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ("Contents/content.hpf", ".hwpx", "application/hwp+zip"),
)

_ODF_MIMES = {
    "application/vnd.oasis.opendocument.text": (".odt", "application/vnd.oasis.opendocument.text"),
    "application/vnd.oasis.opendocument.spreadsheet":
        (".ods", "application/vnd.oasis.opendocument.spreadsheet"),
    "application/vnd.oasis.opendocument.presentation":
        (".odp", "application/vnd.oasis.opendocument.presentation"),
}


def sniff(data: bytes) -> tuple[str, str] | None:
    """(확장자, MIME) 또는 알아보지 못하면 None."""
    if not data:
        return None
    if data[:5] == b"%PDF-":
        return ".pdf", "application/pdf"
    if data[:4] == b"PK\x03\x04":
        return _sniff_zip(data)
    if _looks_textual(data):
        # 평문이면 확장자를 .txt 로 준다. 엔진은 text/plain 으로도 열 수 있지만,
        # 확장자까지 맞춰 주면 둘 중 어느 쪽으로 분기하든 통과한다.
        return ".txt", "text/plain"
    return None


def _sniff_zip(data: bytes) -> tuple[str, str] | None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = set(zf.namelist())
    except Exception:
        return None  # 깨졌거나 우리가 모르는 ZIP. 건드리지 않는다.

    for marker, ext, mime in _ZIP_MARKERS:
        if marker in names:
            return ext, mime
    # ODF 는 'mimetype' 항목에 종류가 문자열로 들어 있다.
    if "mimetype" in names:
        try:
            declared = zf.read("mimetype").decode("ascii", "ignore").strip()
        except Exception:
            return None
        if declared in _ODF_MIMES:
            return _ODF_MIMES[declared]
    return None


def _looks_textual(data: bytes) -> bool:
    """앞부분이 UTF-8 로 읽히고 제어문자가 거의 없으면 평문으로 본다."""
    head = data[:4096]
    if b"\x00" in head:
        return False
    # 4096 경계에서 한글(3바이트)이 잘리면 strict decode 는 그 파일을 통째로
    # "평문 아님" 으로 오판한다. 증분 디코더에 final=False 를 주면 끝에 걸친
    # 잘린 문자만 버퍼에 남기고(오류 아님), 중간의 진짜 깨진 바이트는 여전히
    # 예외로 떨어뜨린다 — 딱 경계 문제만 봐준다.
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return False
    if not text:
        return False
    # 탭·개행·캐리지리턴 말고 제어문자가 섞여 있으면 평문이 아니다.
    odd = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r")
    return odd * 20 <= len(text)  # 5% 미만


def refine(data: bytes, file_name: str, mime_type: str) -> tuple[str, str]:
    """검사에 넘길 (이름, MIME) 을 정한다.

    사이트가 준 이름에 쓸 만한 확장자가 있으면 그대로 둔다 — 사용자가 보는 이름이라
    바꾸지 않는 편이 낫다. 확장자가 없거나 껍데기(.bin)면 바이트를 보고 채운다.
    """
    ext = ("." + file_name.rsplit(".", 1)[1].lower()) if "." in file_name else ""
    if ext and ext != ".bin":
        return file_name, mime_type

    guess = sniff(data)
    if guess is None:
        return file_name, mime_type
    new_ext, new_mime = guess
    stem = file_name.rsplit(".", 1)[0] if "." in file_name else (file_name or "upload")
    return f"{stem}{new_ext}", new_mime
