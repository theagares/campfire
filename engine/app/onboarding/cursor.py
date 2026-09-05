"""
app/onboarding/cursor.py
Cursor 온보딩 (PLAN §4.2 표, "Cursor" 행).

Cursor 의 permissions.json 에는 파일 읽기 차단 키가 없다(mcpAllowlist/
terminalAllowlist/autoRun 뿐). 그래서 설정파일 자동 편집으로는 내장 Read 를 막을
수 없고, 공식 Hooks(beforeReadFile/beforeMCPExecution)로만 가능하다.

**지금은 자동 등록을 하지 않는다.** 훅 설정 파일의 정확한 스키마(위치·필드명)를
공식 문서로 확정하지 못했고, 예전 구현은 그 상태에서 "골격" 훅을 써 넣었다. 그
훅의 command 는 campfire-block-read 였는데 그런 스크립트는 저장소에도 배포
패키지에도 없다 — 적용하면 차단은 안 되면서 사용자 설정만 더럽혀진다. 더 나쁜
것은 사용자가 "우회를 막았다" 고 믿게 된다는 점이다.

스키마가 확정되면 build_action() 이 다시 SettingsDiff 를 돌려주도록 바꾸면 된다.
그 전까지는 손으로 무엇을 해야 하는지만 정직하게 알린다.

안전 수칙: 이 모듈은 어떤 파일도 읽거나 쓰지 않는다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import manual_notice

# 실 사용 시 참고용 기본 경로(버전에 따라 다를 수 있음).
DEFAULT_HOOKS_PATH = Path.home() / ".cursor" / "hooks.json"

# PLAN §4.2 에서 확실한 것은 훅 "이름" 뿐이다 — 파일 위치와 항목 스키마는 미확정.
HOOK_NAMES = ("beforeReadFile", "beforeMCPExecution")

_MANUAL_CHECKLIST = (
    "Cursor 에서 파일 접근은 Campfire MCP 서버로만 하고, 내장 파일 읽기로 문서를 열지 않는다.",
    f"공식 Hooks 를 직접 쓸 수 있다면 {' / '.join(HOOK_NAMES)} 에 차단 훅을 등록한다.",
    "민감 문서는 Cursor 워크스페이스 밖에 두어 내장 Read 의 사정거리에서 뺀다.",
)


def build_action(hooks_path: Path | None = None) -> dict[str, Any]:
    """자동 조치 불가 — 무엇을 손으로 해야 하는지 돌려준다(파일 접근 없음)."""
    return manual_notice(
        "Cursor Hooks 설정의 정확한 스키마가 확정되지 않아 자동 등록을 하지 않는다"
        " — 잘못된 훅을 심으면 막지도 못하면서 막았다고 믿게 된다.",
        list(_MANUAL_CHECKLIST),
        log_path=hooks_path,
    )
