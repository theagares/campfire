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

from .common import MutationError, SettingsDiff, build_diff

# 실 사용 시 참고용 기본 경로 — 사용자가 명시적으로 경로를 넘기지 않을 때만 쓰인다.
# 이 저장소의 테스트/자동검증 코드는 이 기본값을 절대 사용하지 않는다(항상 override).
DEFAULT_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"



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
