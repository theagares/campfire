"""
app/onboarding/cline.py
Cline 온보딩 (PLAN §4.2 표, "Cline (macOS/Linux)" / "Cline (Windows)" 행).

Windows 는 애초에 Hooks 미지원이라 api_conversation_history.json 파싱 기반 사후
경고 + 수동 체크리스트로 대체해 왔다.

**macOS/Linux 의 자동 등록도 지금은 하지 않는다.** 예전 구현은 PreToolUse 훅을
써 넣었는데 그 훅의 command(campfire-block-read)는 저장소에도 배포 패키지에도
없다 — 적용해도 Read 는 그대로 통과하고, 사용자만 "우회를 막았다" 고 믿게 된다.
차단하지 못하는 것보다, 차단했다고 오인시키는 쪽이 보안 제품에서 더 나쁘다.

실제 훅 커맨드 스펙이 확정되고 배포에 실릴 검증 스크립트가 생기면 build_action()
이 다시 SettingsDiff 를 돌려주도록 바꾸면 된다.

autoApprove.readFiles:false 는 승인 프롬프트만 띄울 뿐 진짜 deny 가 아니므로
(§4.2) 참고용 안내로만 다룬다.

안전 수칙: 이 모듈은 어떤 파일도 읽거나 쓰지 않는다(경로는 안내용 문자열).
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

from .common import manual_notice

DEFAULT_SETTINGS_PATH = Path.home() / ".cline" / "settings.json"

_MANUAL_CHECKLIST = [
    "Cline 설정에서 cline.autoApprove.readFiles 를 false 로 둔다"
    " (승인 프롬프트는 뜨지만 사용자가 승인하면 그대로 읽히므로 진짜 차단은 아님, 참고용).",
    "Read 승인 요청이 뜨면 거부하고 secure_read_file MCP 도구를 대신 사용하도록 안내한다.",
    "정기적으로 api_conversation_history.json 의 tool_use(Read) 호출 이력을 점검한다.",
]

_WINDOWS_REASON = "Cline PreToolUse 훅은 macOS/Linux 만 지원한다(Windows 미지원, PLAN §4.2)."
_SPEC_REASON = (
    "Cline PreToolUse 훅의 커맨드 스펙과 배포용 차단 스크립트가 아직 없어 자동 등록을 하지 않는다"
    " — 실행되지 않는 훅을 심으면 막지도 못하면서 막았다고 믿게 된다."
)


def detect_os() -> str:
    return platform.system()  # "Windows" | "Darwin" | "Linux"


def windows_manual_notice(history_path: Path | None = None) -> dict[str, Any]:
    """Windows: Hooks 미지원 -> 로그 파싱 기반 경고 + 수동 체크리스트."""
    return manual_notice(_WINDOWS_REASON, _MANUAL_CHECKLIST, log_path=history_path)


def build_action(
    settings_path: Path | None = None, *, os_name: str | None = None,
    history_path: Path | None = None,
) -> dict[str, Any]:
    """OS 와 무관하게 수동 안내를 돌려준다 — 사유만 다르다(파일 접근 없음)."""
    os_name = os_name or detect_os()
    if os_name == "Windows":
        return windows_manual_notice(history_path)
    return manual_notice(_SPEC_REASON, _MANUAL_CHECKLIST, log_path=history_path)
