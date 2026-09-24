"""
app/core/parser/eml.py
이메일 파싱 — .eml / .mht (PLAN §6). 표준 라이브러리 email 만 쓴다.

메일은 이 게이트웨이가 볼 수 있는 입력 중 PII 밀도가 가장 높다. 본문뿐 아니라
**헤더에 이름·이메일 주소가 그대로** 있고(From/To/Cc), 첨부 파일명에도 사람 이름이
자주 붙는다. 그래서 헤더를 버리지 않고 같이 낸다.

첨부 자체는 풀지 않는다. 메일 안의 DOCX 를 또 파싱하려면 재귀와 zip bomb 방어가
필요해지고, 그건 이 파서의 범위가 아니다 — 첨부는 **이름만** 알려서 사용자가
따로 올리도록 둔다.
"""

from __future__ import annotations

from email import message_from_bytes, policy
from email.message import EmailMessage

_HEADERS = ("From", "To", "Cc", "Bcc", "Reply-To", "Subject", "Date")


def _decode(part) -> str:
    """페이로드를 텍스트로. 헤더의 charset 이 틀린 메일이 흔해서 폴백을 둔다."""
    raw = part.get_payload(decode=True)
    if raw is None:
        return ""
    charset = part.get_content_charset()
    for enc in (charset, "utf-8", "cp949", "euc-kr"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def extract_eml(file_bytes: bytes) -> str:
    msg: EmailMessage = message_from_bytes(file_bytes, policy=policy.default)

    parts: list[str] = []
    for name in _HEADERS:
        value = msg.get(name)
        if value:
            parts.append(f"{name}: {value}")

    bodies: list[str] = []
    attachments: list[str] = []
    html_fallback: list[str] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if filename or part.get_content_disposition() == "attachment":
            # 첨부는 이름만 — 이름 자체에 사람 이름·사번이 들어있는 경우가 많다.
            attachments.append(filename or "(이름 없는 첨부)")
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            bodies.append(_decode(part))
        elif ctype == "text/html":
            html_fallback.append(_decode(part))

    # text/plain 이 있으면 그걸 쓴다. HTML 전용 메일일 때만 태그를 벗겨 쓴다.
    if not bodies and html_fallback:
        from .html import strip_tags

        bodies = [strip_tags(h) for h in html_fallback]

    if attachments:
        parts.append("첨부: " + ", ".join(attachments))
    parts.extend(b.strip() for b in bodies if b.strip())
    return "\n\n".join(parts)
