"""
app/onboarding/cline.py
Cline 온보딩 (PLAN §4.2 표, "Cline (macOS/Linux)" / "Cline (Windows)" 행):
    - macOS/Linux: 공식 PreToolUse 훅(v3.36+) 자동 등록.
    - Windows: Hooks 미지원 -> api_conversation_history.json(Anthropic 네이티브
      tool_use 블록 그대로, 순수 JSON) 파싱 기반 사후 경고 + 수동 체크리스트 안내로 대체.

autoApprove.readFiles:false 는 승인 프롬프트만 띄울 뿐 진짜 deny 가 아니므로
(§4.2) 참고용 안내로만 다룬다.

안전 수칙: settings_path 는 호출자가 넘긴 경로만 사용한다. 테스트는 tempfile
경로만 사용하고, 파일 쓰기는 common.apply_diff(diff, apply=True) 명시 호출
시에만 일어난다. windows_manual_notice() 는 애초에 파일을 쓰지 않는다(읽기용
경로 문자열만 보고서에 표시).
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

from .common import MutationError, SettingsDiff, build_diff, is_our_hook

DEFAULT_SETTINGS_PATH = Path.home() / ".cline" / "settings.json"

_MANUAL_CHECKLIST = [
    "Cline 설정에서 cline.autoApprove.readFiles 를 false 로 둔다"
    " (승인 프롬프트는 뜨지만 사용자가 승인하면 그대로 읽히므로 진짜 차단은 아님, 참고용).",
    "Read 승인 요청이 뜨면 거부하고 secure_read_file MCP 도구를 대신 사용하도록 안내한다.",
    "정기적으로 api_conversation_history.json 의 tool_use(Read) 호출 이력을 점검한다.",
]


def _mutate_register_hooks(after: dict) -> tuple[bool, str]:
    hooks = after.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise MutationError("hooks 필드가 예상한 객체 형식이 아니어서 자동 수정을 건너뜁니다.")
    pre_tool_use = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre_tool_use, list):
        raise MutationError("hooks.PreToolUse 필드가 배열이 아니어서 자동 수정을 건너뜁니다.")

    if any(is_our_hook(e) for e in pre_tool_use):
        return False, "이미 PreToolUse 훅이 등록돼 있습니다."

    # TODO: 실제 Cline PreToolUse 훅 커맨드 스펙 확정되면 command 교체.
    pre_tool_use.append(
        {"matcher": "readFile", "command": "campfire-block-read", "_campfire": True}
    )
    return True, "PreToolUse 훅을 등록해 내장 Read 호출을 실시간 차단합니다(macOS/Linux, PLAN §4.2)."


def build_hooks_diff(settings_path: Path) -> SettingsDiff:
    """macOS/Linux 전용 자동 등록 diff. Windows 에서는 windows_manual_notice() 를 대신 쓴다."""
    return build_diff(Path(settings_path), _mutate_register_hooks)


def windows_manual_notice(history_path: Path | None = None) -> dict[str, Any]:
    """Windows: Hooks 미지원 -> 자동 diff 대신 로그 파싱 기반 경고 + 수동 체크리스트를 반환한다.

    이 함수는 파일을 절대 쓰지 않는다 — history_path 는 "이 경로를 참고해 사후
    점검하라"는 안내용 표시일 뿐이다.
    """
    return {
        "supported": False,
        "reason": "Cline PreToolUse 훅은 macOS/Linux만 지원한다(Windows 미지원, PLAN §4.2).",
        "logPath": str(history_path) if history_path else None,
        "manualChecklist": list(_MANUAL_CHECKLIST),
    }


def detect_os() -> str:
    return platform.system()  # "Windows" | "Darwin" | "Linux"


def build_action(
    settings_path: Path, *, os_name: str | None = None, history_path: Path | None = None
) -> SettingsDiff | dict[str, Any]:
    """OS 분기: macOS/Linux 는 SettingsDiff(자동 등록), Windows 는 수동 안내 dict 를 반환한다."""
    os_name = os_name or detect_os()
    if os_name == "Windows":
        return windows_manual_notice(history_path)
    return build_hooks_diff(settings_path)
