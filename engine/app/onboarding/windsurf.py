"""
app/onboarding/windsurf.py
Windsurf(Devin Desktop) 온보딩 (PLAN §4.2 표, "Windsurf/Devin Desktop" 행).

.codeiumignore 는 경로 차단(best-effort)일 뿐 툴 차단이 아니라(§4.2), 내장 Read 를
막으려면 Cascade Hooks(pre_read_code / pre_mcp_tool_use)뿐이다.

**지금은 자동 등록을 하지 않는다.** cursor.py 와 같은 이유다 — 훅 설정 파일의
정확한 스키마를 확정하지 못한 상태에서 예전 구현은 "골격" 훅을 써 넣었고, 그 훅의
command(campfire-block-read)는 저장소에도 배포 패키지에도 없다. 적용해도 아무것도
막지 못하면서 사용자는 막혔다고 믿게 된다.

스키마가 확정되면 build_action() 이 다시 SettingsDiff 를 돌려주도록 바꾸면 된다.

안전 수칙: 이 모듈은 어떤 파일도 읽거나 쓰지 않는다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import manual_notice

# 실 사용 시 참고용 기본 경로(버전에 따라 다를 수 있음).
DEFAULT_HOOKS_PATH = Path.home() / ".codeium" / "windsurf" / "hooks.json"

# PLAN §4.2 에서 확실한 것은 훅 "이름" 뿐이다 — 파일 위치와 항목 스키마는 미확정.
HOOK_NAMES = ("pre_read_code", "pre_mcp_tool_use")

_MANUAL_CHECKLIST = (
    "Windsurf 에서 파일 접근은 Campfire MCP 서버로만 하고, Cascade 내장 읽기로 문서를 열지 않는다.",
    f"Cascade Hooks 를 직접 쓸 수 있다면 {' / '.join(HOOK_NAMES)} 에 차단 훅(exit code 2)을 등록한다.",
    ".codeiumignore 는 경로 힌트일 뿐 툴 차단이 아니므로 이것만 믿지 않는다.",
)


def build_action(hooks_path: Path | None = None) -> dict[str, Any]:
    """자동 조치 불가 — 무엇을 손으로 해야 하는지 돌려준다(파일 접근 없음)."""
    return manual_notice(
        "Cascade Hooks 설정의 정확한 스키마가 확정되지 않아 자동 등록을 하지 않는다"
        " — 잘못된 훅을 심으면 막지도 못하면서 막았다고 믿게 된다.",
        list(_MANUAL_CHECKLIST),
        log_path=hooks_path,
    )
