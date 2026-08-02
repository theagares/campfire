"""
app/onboarding/windsurf.py
Windsurf(Devin Desktop) 온보딩 (PLAN §4.2 표, "Windsurf/Devin Desktop" 행):
    Cascade Hooks pre_read_code / pre_mcp_tool_use 를 자동 등록해 exit code 2 로
    내장 Read 호출을 실시간 차단한다. .codeiumignore 는 경로 차단(best-effort)일
    뿐 툴 차단이 아니라서(§4.2) 설정파일 자동 편집이 아니라 공식 Hooks 등록을 쓴다.

cursor.py 와 마찬가지로, 정확한 훅 설정 파일 스키마는 이 구현 시점에 공식 문서로
100% 확정 검증하지 못했다 — PLAN §4.2 에 명시된 훅 이름만 확실하므로 골격 형태로
구현해두고, 스펙이 확정되면 _mutate_register_hooks() 내부만 교체하면 된다.

안전 수칙: hooks_path 는 호출자가 넘긴 경로만 사용한다. 테스트는 tempfile 경로만
사용하고, 파일 쓰기는 common.apply_diff(diff, apply=True) 명시 호출 시에만 일어난다.
"""

from __future__ import annotations

from pathlib import Path

from .common import MutationError, SettingsDiff, build_diff, is_our_hook

# 실 사용 시 참고용 기본 경로 — 확정 전까지는 호출자가 명시적으로 경로를 넘기는
# 것을 권장한다. 테스트는 이 기본값을 절대 사용하지 않는다.
DEFAULT_HOOKS_PATH = Path.home() / ".codeium" / "windsurf" / "hooks.json"

_HOOK_NAMES = ("pre_read_code", "pre_mcp_tool_use")


def _mutate_register_hooks(after: dict) -> tuple[bool, str]:
    hooks = after.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise MutationError("hooks 필드가 예상한 객체 형식이 아니어서 자동 수정을 건너뜁니다.")

    added: list[str] = []
    for hook_name in _HOOK_NAMES:
        entries = hooks.setdefault(hook_name, [])
        if not isinstance(entries, list):
            raise MutationError(f"hooks.{hook_name} 필드가 배열이 아니어서 자동 수정을 건너뜁니다.")
        if any(is_our_hook(e) for e in entries):
            continue  # 이미 등록됨
        # TODO: 실제 Cascade Hooks 커맨드 스펙 확정되면 command 교체. exitCodeBlock=2 로 차단.
        entries.append(
            {"command": "campfire-block-read", "exitCodeBlock": 2, "_campfire": True}
        )
        added.append(hook_name)

    if not added:
        return False, "이미 pre_read_code/pre_mcp_tool_use 훅이 등록돼 있습니다."
    return True, f"{', '.join(added)} 훅을 등록해 exit code 2 로 내장 Read 호출을 실시간 차단합니다(PLAN §4.2)."


def build_hooks_diff(hooks_path: Path) -> SettingsDiff:
    """Cascade Hooks 설정에 pre_read_code/pre_mcp_tool_use 등록 diff 를 생성한다(적용 안 함)."""
    return build_diff(Path(hooks_path), _mutate_register_hooks)
