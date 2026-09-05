"""
app/onboarding/vscode_copilot.py
VS Code Copilot Chat 온보딩 (PLAN §4.2 표, "VS Code Copilot Chat" 행):
    chatSessions/*.jsonl(toolInvocationSerialized) 파싱 병행(저장 누락 버그,
    GH #285535 등이 있어 카운트를 완전히 신뢰하지 않는다)
    + 수동 체크리스트(커스텀 Chat Mode 로 기본 Agent 모드에서 파일 read 툴 제외).

**Agent Hooks(Preview) 자동 등록은 하지 않는다.** 예전 구현은 Preview 스펙이
확정되지 않은 상태에서 chat.agent.hooks 아래에 훅을 써 넣었는데, 그 훅의
command(campfire-block-read)는 저장소에도 배포 패키지에도 없다. 키 구조도 추정이라
VS Code 가 무시할 가능성이 높다 — 결국 사용자 설정만 더럽히고 차단은 안 되면서,
"등록됐다" 는 보고만 남는다.

Preview 스펙이 확정되고 배포용 차단 스크립트가 생기면 build_action() 이 등록 diff 를
함께 돌려주도록 바꾸면 된다.

안전 수칙: 이 모듈은 어떤 파일도 읽거나 쓰지 않는다(경로는 안내용 문자열).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import manual_notice

DEFAULT_SETTINGS_PATH = Path.home() / "AppData" / "Roaming" / "Code" / "User" / "settings.json"

_MANUAL_CHECKLIST = [
    "커스텀 Chat Mode(*.chatmode.md)를 만들어 기본 Agent 모드에서 파일 read 툴을 제외한다"
    "(기본 모드엔 영속 설정이 없음, PLAN §4.2).",
    "chatSessions/*.jsonl 의 toolInvocationSerialized 항목을 주기적으로 점검한다"
    "(저장 누락 버그(GH #285535 등)로 카운트를 완전히 신뢰하지 않는다).",
    "가능하면 secure_read_file MCP 도구를 기본 Read 대신 쓰도록 팀 규칙으로 안내한다.",
]

_SPEC_REASON = (
    "Agent Hooks 는 아직 Preview 이고 배포용 차단 스크립트도 없어 자동 등록을 하지 않는다"
    " — 추정 스키마로 심은 훅은 무시되거나 실행에 실패한다."
)


def manual_checklist() -> list[str]:
    return list(_MANUAL_CHECKLIST)


def build_action(settings_path: Path | None = None, log_glob_path: Path | None = None) -> dict[str, Any]:
    """자동 조치 불가 — 로그 점검 경로와 수동 체크리스트를 돌려준다(파일 접근 없음)."""
    return manual_notice(_SPEC_REASON, _MANUAL_CHECKLIST, log_path=log_glob_path)
