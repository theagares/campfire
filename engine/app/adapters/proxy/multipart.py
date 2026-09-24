"""
app/adapters/proxy/multipart.py
multipart/form-data 본문에서 파일 파트만 찾아 바꿔 끼우는 최소 도구.

mitmproxy 에도 multipart 파서가 있지만 쓰지 않는다. `request.multipart_form` 은
필드명 → 값 의 평면 딕셔너리라 **파트 헤더를 잃는다** — 우리는 filename 과
Content-Type 을 그대로 두거나 의도적으로 바꿔야 하므로 그 정보가 필요하다.
raw 본문을 직접 다루면 mitmproxy 버전 간 API 변화에도 안 흔들린다.

ponytail: 경계 문자열이 파트 본문 안에 그대로 들어 있는 경우는 다루지 않는다.
RFC 상 보내는 쪽이 피해야 하는 값이고 실제 브라우저는 난수 경계를 쓴다. 문제가
관측되면 경계 재생성으로 올라가면 된다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FILENAME_RE = re.compile(rb'filename\s*=\s*"((?:[^"\\]|\\.)*)"', re.IGNORECASE)
_CT_RE = re.compile(rb"^content-type\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


class MultipartError(Exception):
    """본문이 multipart 로 읽히지 않는다. 호출부는 fail-closed 로 처리한다."""


@dataclass
class Part:
    """multipart 한 조각. headers 는 raw 바이트 그대로 보관한다."""

    headers: bytes
    body: bytes

    @property
    def filename(self) -> str | None:
        m = _FILENAME_RE.search(self.headers)
        if not m:
            return None
        return m.group(1).decode("utf-8", "replace")

    @property
    def content_type(self) -> str:
        m = _CT_RE.search(self.headers)
        return m.group(1).decode("latin-1") if m else ""

    def is_file(self) -> bool:
        # filename 이 있으면 파일 파트다. 빈 문자열(filename="")은 "파일 안 고름"
        # 이라 파일로 세지 않는다 — 이걸 파일로 보면 0바이트를 스캔하게 된다.
        return bool(self.filename)

    def replaced(self, *, body: bytes, filename: str, content_type: str) -> "Part":
        """본문과 파일명/타입을 바꾼 새 파트.

        파일명을 바꾸는 이유: 마스킹 결과는 원본 포맷이 아니라 텍스트(MD)다.
        `.docx` 라고 적힌 채 MD 바이트를 보내면 받는 쪽이 DOCX 로 열려다 실패한다.
        """
        headers = _FILENAME_RE.sub(
            b'filename="' + filename.encode("utf-8") + b'"', self.headers, count=1
        )
        if _CT_RE.search(headers):
            headers = _CT_RE.sub(
                b"Content-Type: " + content_type.encode("latin-1"), headers, count=1
            )
        else:
            headers = headers.rstrip(b"\r\n") + b"\r\nContent-Type: " + content_type.encode("latin-1")
        return Part(headers=headers, body=body)


def boundary_of(content_type: str) -> bytes:
    """Content-Type 헤더에서 boundary 를 꺼낸다."""
    m = re.search(r'boundary\s*=\s*"?([^";]+)"?', content_type or "", re.IGNORECASE)
    if not m:
        raise MultipartError(f"boundary 없음: {content_type!r}")
    return m.group(1).strip().encode("latin-1")


def decode(content: bytes, content_type: str) -> list[Part]:
    """본문을 파트 목록으로 가른다. 순서를 보존한다."""
    boundary = boundary_of(content_type)
    sep = b"--" + boundary
    chunks = content.split(sep)
    parts: list[Part] = []
    # 첫 조각은 preamble, 마지막은 "--\r\n" 꼬리라 둘 다 버린다.
    for chunk in chunks[1:-1]:
        chunk = chunk.lstrip(b"\r\n")
        head, sp, body = chunk.partition(b"\r\n\r\n")
        if not sp:
            continue
        # 경계 앞의 CRLF 는 **정확히 하나만** 떼야 한다. rstrip 으로 싹 지우면
        # 개행으로 끝나는 파일의 마지막 바이트가 사라지고, 바이너리면 그대로 손상이다.
        if body.endswith(b"\r\n"):
            body = body[:-2]
        parts.append(Part(headers=head, body=body))
    if not parts:
        raise MultipartError("파트를 찾지 못했다")
    return parts


def encode(parts: list[Part], content_type: str) -> bytes:
    """파트 목록을 다시 본문으로. 들어올 때와 같은 boundary 를 쓴다."""
    boundary = boundary_of(content_type)
    sep = b"--" + boundary
    out = bytearray()
    for p in parts:
        out += sep + b"\r\n" + p.headers + b"\r\n\r\n" + p.body + b"\r\n"
    out += sep + b"--\r\n"
    return bytes(out)
