"""app/core/parser/txt.py — TXT 파싱(내장). UTF-8 우선, 실패 시 cp949 폴백."""

from __future__ import annotations


def extract_txt(file_bytes: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp949", "euc-kr"):
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    # 최후: 손실 허용 디코딩
    return file_bytes.decode("utf-8", errors="replace")
