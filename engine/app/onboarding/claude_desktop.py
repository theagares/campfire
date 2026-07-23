"""
app/onboarding/claude_desktop.py
Claude Desktop 온보딩 (PLAN §4.2 표, "Claude Desktop" 행):
    내장 파일 read 툴 자체가 없음(파일 접근은 사용자가 붙인 filesystem MCP
    서버뿐) -> "툴 차단"이 아니라 claude_desktop_config.json 의 mcpServers 에
    서드파티 filesystem 서버가 등록돼 있는지만 읽어서 감지하고, 있으면 경고
    메시지만 생성한다. 제거는 사용자 승인이 필요하므로 이 모듈은 **파일을 절대
    수정하지 않는다**(읽기 전용).

안전 수칙: config_path 는 호출자가 넘긴 경로만 사용한다. 테스트는 tempfile
경로만 사용한다. 이 모듈에는 apply 관련 함수가 아예 없다 — 쓰기 기능 자체가 없음.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import load_json

DEFAULT_CONFIG_PATH = Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"

# 서버 이름/커맨드/인자에 이 키워드가 있으면 "파일시스템 접근 가능 서버"로 간주(휴리스틱).
_FILESYSTEM_KEYWORDS = ("filesystem", "file-system", "file_system", "fs-server", "files")


def _looks_like_filesystem_server(name: str, entry: dict) -> bool:
    args = entry.get("args", [])
    args_text = " ".join(str(a) for a in args) if isinstance(args, list) else ""
    haystack = " ".join([name, str(entry.get("command", "")), args_text]).lower()
    return any(kw in haystack for kw in _FILESYSTEM_KEYWORDS)


def detect_filesystem_servers(config_path: Path) -> dict[str, Any]:
    """claude_desktop_config.json 을 읽기 전용으로 감지한다(수정 없음).

    반환: {path, exists, error, servers(감지된 서버 이름 목록), warning(있으면 경고 문구)}.
    """
    config_path = Path(config_path)
    data, error = load_json(config_path)

    if error is not None:
        return {"path": str(config_path), "exists": config_path.exists(), "error": error, "servers": [], "warning": None}
    if data is None:
        return {"path": str(config_path), "exists": False, "error": None, "servers": [], "warning": None}

    mcp_servers = data.get("mcpServers", {})
    found: list[str] = []
    if isinstance(mcp_servers, dict):
        for name, entry in mcp_servers.items():
            if isinstance(entry, dict) and _looks_like_filesystem_server(str(name), entry):
                found.append(str(name))

    warning = None
    if found:
        warning = (
            f"서드파티 filesystem MCP 서버({', '.join(found)})가 등록돼 있습니다. "
            "이 서버로 원본 파일을 직접 읽으면 securedoc-gateway 를 우회할 수 있습니다 — "
            "제거는 사용자 승인이 필요하므로 자동으로 삭제하지 않았습니다. 직접 확인 후 제거를 권장합니다."
        )
    return {"path": str(config_path), "exists": True, "error": None, "servers": found, "warning": warning}
