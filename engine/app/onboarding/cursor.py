"""
app/onboarding/cursor.py
Cursor 온보딩 (PLAN §4.2 표, "Cursor" 행):
    beforeReadFile / beforeMCPExecution 공식 Hooks(v1.7+)를 자동 등록해 내장
    Read 호출을 실시간 차단한다. permissions.json 에는 파일 읽기 차단 키가 없어
    (mcpAllowlist/terminalAllowlist/autoRun 뿐) 설정파일 자동 편집으로는 불가능
    하므로(§4.2), 공식 Hooks 등록 메커니즘을 쓴다.

정확한 훅 설정 파일의 스키마(정확한 파일 위치·필드명)는 이 구현 시점에 공식
문서로 100% 확정 검증하지 못했다 — PLAN §4.2 에 명시된 훅 이름만 확실하다.
그래서 여기서는 "여기에 훅을 등록한다"는 골격을 실행 가능한 diff 생성 함수로
구현해두고, 정확한 스키마가 확정되면 _mutate_register_hooks() 내부만 교체하면
되도록 설계했다.

안전 수칙: hooks_path 는 호출자가 넘긴 경로만 사용한다. 테스트는 tempfile 경로만
사용하고, 파일 쓰기는 common.apply_diff(diff, apply=True) 명시 호출 시에만 일어난다.
"""

from __future__ import annotations

from pathlib import Path

from .common import MutationError, SettingsDiff, build_diff, is_our_hook

# 실 사용 시 참고용 기본 경로(버전에 따라 다를 수 있음) — 확정 전까지는 호출자가
# 명시적으로 경로를 넘기는 것을 권장한다. 테스트는 이 기본값을 절대 사용하지 않는다.
DEFAULT_HOOKS_PATH = Path.home() / ".cursor" / "hooks.json"

_HOOK_NAMES = ("beforeReadFile", "beforeMCPExecution")


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
        # TODO: 실제 Cursor Hooks 커맨드 스펙(문서) 확정되면 command 를 교체.
        entries.append({"command": "campfire-block-read", "_campfire": True})
        added.append(hook_name)

    if not added:
        return False, "이미 beforeReadFile/beforeMCPExecution 훅이 등록돼 있습니다."
    return True, f"{', '.join(added)} 훅을 등록해 내장 Read 호출을 실시간 차단합니다(PLAN §4.2)."


def build_hooks_diff(hooks_path: Path) -> SettingsDiff:
    """Cursor Hooks 설정에 beforeReadFile/beforeMCPExecution 등록 diff 를 생성한다(적용 안 함)."""
    return build_diff(Path(hooks_path), _mutate_register_hooks)
