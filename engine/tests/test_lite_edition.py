"""라이트 판(SECUREDOC_EDITION=lite) — PII·인젝션·사용자 지정 강제 마스킹만 남는다.

데스크탑 버전에 -lite 가 붙으면 앱이 SECUREDOC_EDITION=lite 로 엔진을 띄운다(desktop/main/edition.js).
정식 판에서 켜 둔 설정(기밀 탐지·위험 검사기·프록시 자동 시작)이 남은 채 라이트로 바꿔 설치해도
엔진이 켜지 않아야 하고, 프록시 토글 API·MCP 서버도 열리지 않아야 한다.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
from pathlib import Path

from app import config

_ENGINE_DIR = Path(__file__).resolve().parents[1]
_FLAGS = {
    "SECUREDOC_CONFIDENTIAL_ENABLED": "1",
    "SECUREDOC_MCP_RISK_SCANNER_ENABLED": "1",
    "SECUREDOC_DEMO_TRACE": "1",
    "SECUREDOC_PROXY_ENABLED": "1",
}


def test_lite_turns_off_features_even_if_settings_left_on(monkeypatch):
    for key, value in _FLAGS.items():
        monkeypatch.setenv(key, value)
    try:
        monkeypatch.setenv("SECUREDOC_EDITION", "full")
        full = importlib.reload(config)
        assert full.CONFIDENTIAL_ENABLED and full.MCP_RISK_SCANNER_ENABLED
        assert full.DEMO_TRACE_ENABLED and full.PROXY_ENABLED and not full.LITE

        monkeypatch.setenv("SECUREDOC_EDITION", "lite")
        lite = importlib.reload(config)
        assert lite.LITE
        assert not (lite.CONFIDENTIAL_ENABLED or lite.MCP_RISK_SCANNER_ENABLED
                    or lite.DEMO_TRACE_ENABLED or lite.PROXY_ENABLED)
    finally:
        for key in [*_FLAGS, "SECUREDOC_EDITION"]:
            monkeypatch.delenv(key, raising=False)
        importlib.reload(config)  # 다른 테스트가 쓰는 상태로 되돌린다


def test_lite_refuses_proxy_toggle(monkeypatch):
    from app.adapters import proxy

    monkeypatch.setattr(config, "LITE", True)
    assert asyncio.run(proxy.start(force=True)) is False  # 토글(force) 경로도 막는다
    assert not proxy.is_running()
    assert proxy.status()["supported"] is False  # /health 도 "지원" 이라고 하지 않는다


def _mcp_mounted(edition: str, tmp_path) -> bool:
    env = {**os.environ, "SECUREDOC_EDITION": edition, "SECUREDOC_STORE_DIR": str(tmp_path)}
    code = ("from app.main import app; "
            "print(any(getattr(r, 'path', '').startswith('/mcp') for r in app.routes))")
    out = subprocess.run([sys.executable, "-c", code], cwd=_ENGINE_DIR, env=env,
                         capture_output=True, text=True, timeout=120, check=True)
    return out.stdout.strip().splitlines()[-1] == "True"


def test_lite_does_not_open_mcp_server(tmp_path):
    assert _mcp_mounted("full", tmp_path)
    assert not _mcp_mounted("lite", tmp_path)
