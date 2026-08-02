"""
app/onboarding/vscode_copilot.py
VS Code Copilot Chat 온보딩 (PLAN §4.2 표, "VS Code Copilot Chat" 행):
    Agent Hooks(Preview) 등록 시도 + chatSessions/*.jsonl(toolInvocationSerialized)
    파싱 병행(저장 누락 버그, GH #285535 등, 있어 카운트를 완전히 신뢰하지 않음)
    + 수동 체크리스트(커스텀 Chat Mode 로 기본 Agent 모드에서 파일 read 툴 제외).

Agent Hooks 는 아직 Preview 라 스펙 변경 가능성이 있어 의존도를 낮춘다 — 그래서
등록 diff 를 만들되(성공하든 실패하든) 수동 체크리스트를 항상 함께 반환한다.

안전 수칙: settings_path 는 호출자가 넘긴 경로만 사용한다. 테스트는 tempfile
경로만 사용하고, 파일 쓰기는 common.apply_diff(diff, apply=True) 명시 호출
시에만 일어난다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import MutationError, SettingsDiff, build_diff, is_our_hook

DEFAULT_SETTINGS_PATH = Path.home() / "AppData" / "Roaming" / "Code" / "User" / "settings.json"

_MANUAL_CHECKLIST = [
    "커스텀 Chat Mode(*.chatmode.md)를 만들어 기본 Agent 모드에서 파일 read 툴을 제외한다"
    "(기본 모드엔 영속 설정이 없음, PLAN §4.2).",
    "chatSessions/*.jsonl 의 toolInvocationSerialized 항목을 주기적으로 점검한다"
    "(저장 누락 버그(GH #285535 등)로 카운트를 완전히 신뢰하지 않는다).",
    "가능하면 secure_read_file MCP 도구를 기본 Read 대신 쓰도록 팀 규칙으로 안내한다.",
]

_HOOK_ROOT_KEY = "chat.agent.hooks"  # TODO: 실제 Preview 스펙 확정되면 키 교체


def _mutate_register_agent_hook(after: dict) -> tuple[bool, str]:
    hooks = after.setdefault(_HOOK_ROOT_KEY, {})
    if not isinstance(hooks, dict):
        raise MutationError(f"{_HOOK_ROOT_KEY} 필드가 예상한 객체 형식이 아니어서 자동 수정을 건너뜁니다.")
    pre_tool_use = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre_tool_use, list):
        raise MutationError(f"{_HOOK_ROOT_KEY}.PreToolUse 필드가 배열이 아니어서 자동 수정을 건너뜁니다.")

    if any(is_our_hook(e) for e in pre_tool_use):
        return False, "이미 Agent Hooks(Preview) 등록이 있습니다."

    # TODO: 실제 Agent Hooks(Preview) 스펙 확정되면 command/키 구조 교체.
    pre_tool_use.append(
        {"matcher": "readFile", "command": "campfire-block-read", "_campfire": True}
    )
    return True, "Agent Hooks(Preview) 에 Read 차단을 등록 시도합니다(스펙 변경 가능성 있어 수동 체크리스트 병행 필수)."


def build_agent_hook_diff(settings_path: Path) -> SettingsDiff:
    """Agent Hooks(Preview) 등록 diff 를 생성한다(적용 안 함). Preview 라 신뢰도 낮음."""
    return build_diff(Path(settings_path), _mutate_register_agent_hook)


def manual_checklist() -> list[str]:
    return list(_MANUAL_CHECKLIST)


def build_action(settings_path: Path, log_glob_path: Path | None = None) -> dict[str, Any]:
    """Agent Hooks 등록 diff + 로그 파싱 경로 안내 + 수동 체크리스트를 종합한다."""
    diff = build_agent_hook_diff(settings_path)
    return {
        "hookDiff": diff.to_dict(),
        "logPath": str(log_glob_path) if log_glob_path else None,
        "manualChecklist": manual_checklist(),
    }
