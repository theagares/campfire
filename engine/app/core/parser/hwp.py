"""
app/core/parser/hwp.py
HWP 파싱 — LibreOffice 로 DOCX 변환 후 기존 docx.py 파서 재사용 (PLAN §6).

LibreOffice 는 앱에 번들하지 않는다(PLAN §11 "LibreOffice 의존" 리스크) —
미설치 환경에서는 실행 파일을 찾지 못하므로 예외를 던지지 않고
NotImplementedError 로 변환해 "미지원 통과"(PLAN §9.2) 로 처리한다.
(참고: 파이프라인/server/pipeline/parsers/hwp.py 의 변환 커맨드를 이식하되,
실행 파일 탐색만 Windows 대응으로 확장.)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

from .docx import extract_docx

# Windows 일반 설치 경로 (PATH 에 없을 때의 폴백)
_WINDOWS_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]


def _find_soffice() -> str | None:
    """OS별 LibreOffice 실행 파일 탐색.

    Windows: PATH 의 soffice.exe 우선, 없으면 일반 설치 경로.
    그 외 OS: PATH 의 soffice/libreoffice.
    """
    if sys.platform.startswith("win"):
        found = shutil.which("soffice.exe") or shutil.which("soffice")
        if found:
            return found
        for candidate in _WINDOWS_CANDIDATES:
            if os.path.exists(candidate):
                return candidate
        return None

    return shutil.which("soffice") or shutil.which("libreoffice")


def extract_hwp(file_bytes: bytes) -> str:
    soffice = _find_soffice()
    if not soffice:
        raise NotImplementedError(
            "HWP 는 LibreOffice 가 설치되어 있지 않아 지원하지 않습니다 "
            "(변환기 미설치 — 앱에 LibreOffice 를 번들하지 않음, PLAN §11)."
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        hwp_path = os.path.join(tmpdir, "input.hwp")
        with open(hwp_path, "wb") as f:
            f.write(file_bytes)

        try:
            result = subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--norestore",
                    "--convert-to",
                    "docx",
                    "--outdir",
                    tmpdir,
                    hwp_path,
                ],
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            # 탐색은 됐으나 실행 자체가 실패(권한 등) — 미지원으로 우아하게 처리
            raise NotImplementedError(
                f"LibreOffice 실행에 실패해 HWP 를 지원할 수 없습니다: {exc}"
            ) from exc

        if result.returncode != 0:
            raise RuntimeError(
                f"LibreOffice HWP 변환 실패: {result.stderr.decode(errors='replace')}"
            )

        docx_path = os.path.join(tmpdir, "input.docx")
        if not os.path.exists(docx_path):
            raise RuntimeError("LibreOffice 가 DOCX 파일을 생성하지 못했습니다.")

        with open(docx_path, "rb") as f:
            docx_bytes = f.read()

    return extract_docx(docx_bytes)
