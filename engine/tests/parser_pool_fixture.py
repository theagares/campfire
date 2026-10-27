"""Pickle/import-safe parser used by process-pool lifecycle tests."""

from __future__ import annotations

import time


def parse_for_test(
    file_bytes: bytes,
    _mime_type: str,
    _file_name: str,
    images: list[bytes] | None = None,
) -> tuple[str, str, None]:
    delay_text, separator, body = file_bytes.decode("utf-8").partition("|")
    time.sleep(float(delay_text))
    if images is not None:
        images.append(b"fixture-image")
    return (body if separator else delay_text), "ok", None
