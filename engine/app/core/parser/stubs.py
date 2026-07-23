"""
app/core/parser/stubs.py
U5 범위 밖(레거시 바이너리 포맷) 스텁 (PLAN §6, §10 Phase 5).

HWP/HWPX/XLSX/PPTX 는 U5 에서 실제 파서로 교체 완료(hwp.py/hwpx.py/xlsx.py/pptx.py).
구버전 바이너리 포맷(XLS/PPT)은 이번 범위 밖이라 계속 "미지원" 사유만 돌려준다.
"""

from __future__ import annotations

_REASONS = {
    ".xls": "XLS(구버전 바이너리 포맷)는 지원하지 않습니다 (XLSX 만 지원).",
    ".ppt": "PPT(구버전 바이너리 포맷)는 지원하지 않습니다 (PPTX 만 지원).",
}


def unsupported_reason(ext: str) -> str:
    return _REASONS.get(ext, f"지원하지 않는 포맷입니다: {ext} (U5 예정).")
