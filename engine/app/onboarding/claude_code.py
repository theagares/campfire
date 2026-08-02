"""
app/onboarding/claude_code.py
Claude Code 온보딩 (PLAN §4.2 표, "Claude Code" 행):
    설정 파일에 permissions.deny: ["Read"] 자동 추가 (diff 표시 -> 사용자 확인 후 적용)
    + 가능하면 PreToolUse 훅도 함께 등록(이중 방어 — permissions.deny 강제 버그
    이력, GH #24846 등 대비).

안전 수칙(필독): settings_path 는 반드시 호출자가 넘긴 경로만 사용한다. 이
저장소의 테스트는 절대 실제 ~/.claude/settings.json 을 대상으로 호출하지 않고
tempfile 기반 임시 경로만 넘긴다. 아래 build_*_diff() 함수는 diff 생성만 하고
파일을 절대 쓰지 않는다 — 실제 적용은 common.apply_diff(diff, apply=True) 를
명시적으로 호출했을 때만 일어난다.
"""

from __future__ import annotations

from pathlib import Path

from .common import MutationError, SettingsDiff, build_diff, is_our_hook

# 실 사용 시 참고용 기본 경로 — 사용자가 명시적으로 경로를 넘기지 않을 때만 쓰인다.
# 이 저장소의 테스트/자동검증 코드는 이 기본값을 절대 사용하지 않는다(항상 override).
DEFAULT_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

_PRETOOLUSE_HOOK_MATCHER = "Read"


def _mutate_deny_read(after: dict) -> tuple[bool, str]:
    permissions = after.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise MutationError("permissions 필드가 예상한 객체 형식이 아니어서 자동 수정을 건너뜁니다.")
    deny = permissions.setdefault("deny", [])
    if not isinstance(deny, list):
        raise MutationError("permissions.deny 필드가 배열이 아니어서 자동 수정을 건너뜁니다.")

    if "Read" in deny:
        return False, '이미 permissions.deny 에 "Read" 가 있어 변경할 내용이 없습니다.'

    deny.append("Read")
    return True, 'permissions.deny 에 "Read" 를 추가해 내장 Read 도구를 차단합니다(PLAN §4.2).'


def build_deny_read_diff(settings_path: Path) -> SettingsDiff:
    """settings.json 의 permissions.deny 에 "Read" 를 추가하는 diff 를 생성한다(적용 안 함).

    파일이 없으면 새로 만들 diff(permissions.deny: ["Read"] 뼈대 포함)를 생성한다.
    JSON 파싱 실패 등 엣지케이스는 error 필드로 표시하고 after=None 을 반환한다.
    """
    return build_diff(Path(settings_path), _mutate_deny_read)


def _mutate_pretooluse_hook(after: dict) -> tuple[bool, str]:
    hooks = after.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise MutationError("hooks 필드가 예상한 객체 형식이 아니어서 자동 수정을 건너뜁니다.")
    pre_tool_use = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre_tool_use, list):
        raise MutationError("hooks.PreToolUse 필드가 배열이 아니어서 자동 수정을 건너뜁니다.")

    for entry in pre_tool_use:
        if isinstance(entry, dict) and entry.get("matcher") == _PRETOOLUSE_HOOK_MATCHER:
            for h in entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []:
                if is_our_hook(h):
                    return False, "이미 campfire PreToolUse 이중 방어 훅이 등록돼 있습니다."

    # 이중 방어용 훅: permissions.deny 강제가 실패하는 버그 이력(GH #24846 등) 대비.
    # command 는 실제 배포 패키지에 포함될 검증 스크립트 경로로 교체해야 한다(TODO) —
    # 여기서는 "Read 호출을 감지해 차단 신호(exit code 2)를 보낸다"는 골격만 남긴다.
    pre_tool_use.append(
        {
            "matcher": _PRETOOLUSE_HOOK_MATCHER,
            "hooks": [
                {
                    "type": "command",
                    "command": "campfire-block-read",  # TODO: 실제 배포 스크립트 경로로 교체
                    "_campfire": True,
                }
            ],
        }
    )
    return True, "hooks.PreToolUse 에 Read 차단 이중 방어 훅을 등록합니다(PLAN §4.2)."


def build_pretooluse_hook_diff(settings_path: Path) -> SettingsDiff:
    """permissions.deny 강제 실패에 대비한 PreToolUse 훅 이중 방어 diff 를 생성한다."""
    return build_diff(Path(settings_path), _mutate_pretooluse_hook)
