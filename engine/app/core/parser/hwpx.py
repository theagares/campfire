"""
app/core/parser/hwpx.py
HWPX 파싱 — pyhwpx (PLAN §6).

pyhwpx 는 실제 한/글(한컴오피스) 프로그램을 COM(HWPFrame.HwpObject)으로
자동화하는 방식이라, 시스템에 한/글이 설치되어 있어야 동작한다(다른 오피스
스위트로는 대체 불가). 한/글 미설치 환경에서는 COM Dispatch 자체가 실패하므로,
그 경우 예외를 밖으로 던지지 않고 NotImplementedError 로 변환해 "미지원 통과"
(PLAN §9.2) 로 처리한다 — hwp.py 의 LibreOffice 미설치 처리와 동일한 원칙.
"""

from __future__ import annotations

import os
import tempfile


def extract_hwpx(file_bytes: bytes) -> str:
    try:
        from pyhwpx import Hwp
    except ImportError as exc:
        raise NotImplementedError(f"pyhwpx 라이브러리를 사용할 수 없습니다: {exc}") from exc

    tmp_path: str | None = None
    hwp = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".hwpx")
        os.close(fd)
        with open(tmp_path, "wb") as f:
            f.write(file_bytes)

        try:
            hwp = Hwp(new=True, visible=False, register_module=True)
        except Exception as exc:  # noqa: BLE001 - COM Dispatch 실패(한/글 미설치 등)
            raise NotImplementedError(
                f"한/글(한컴오피스) 프로그램이 설치되어 있지 않아 HWPX 를 지원하지 않습니다: {exc}"
            ) from exc

        opened = hwp.open(tmp_path)
        if not opened:
            raise RuntimeError("HWPX 파일을 여는 데 실패했습니다.")

        parts: list[str] = []
        hwp.init_scan()
        try:
            while True:
                state, text = hwp.get_text()
                if state <= 1:
                    break
                if text:
                    parts.append(text)
        finally:
            hwp.release_scan()

        return "".join(parts)
    finally:
        if hwp is not None:
            try:
                hwp.quit(save=False)
            except Exception:  # noqa: BLE001
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
