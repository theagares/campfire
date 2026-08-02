"""
MCP 우회 방지 온보딩 체크리스트 도구 검증 (PLAN §4.2, §10 Phase 6).

안전 수칙: 이 테스트 파일의 모든 함수 호출은 tempfile.mkdtemp() 로 만든 임시
디렉토리만 대상 경로로 넘긴다. 실제 사용자 홈 디렉토리(~/.claude/ 등)는
어떤 테스트에서도 절대 참조하지 않는다 — DEFAULT_* 상수(Path.home() 기반)는
import 만 되고 인자로 넘기지는 않는다.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.onboarding import checklist, claude_code, claude_desktop, cline, cursor, vscode_copilot, windsurf
from app.onboarding.common import apply_diff


@pytest.fixture()
def tmp_home(tmp_path: Path) -> Path:
    """pytest 표준 tmp_path 를 그대로 쓴다(내부적으로 tempfile 기반 임시 디렉토리) —
    실제 홈 디렉토리와는 완전히 무관한 격리된 경로."""
    return tmp_path


# ── claude_code ───────────────────────────────────────────────────────────────


def test_claude_code_deny_read_diff_new_file(tmp_home):
    settings_path = tmp_home / "settings.json"
    assert not settings_path.exists()

    diff = claude_code.build_deny_read_diff(settings_path)
    assert diff.changed is True
    assert diff.exists_before is False
    assert diff.after["permissions"]["deny"] == ["Read"]
    assert diff.error is None
    # dry-run: 파일이 실제로 생성되지 않아야 한다
    assert not settings_path.exists()


def test_claude_code_deny_read_diff_apply_writes_only_to_tmp(tmp_home):
    settings_path = tmp_home / "settings.json"
    diff = claude_code.build_deny_read_diff(settings_path)

    applied = apply_diff(diff, apply=True)
    assert applied is True
    assert settings_path.exists()

    written = json.loads(settings_path.read_text(encoding="utf-8"))
    assert written["permissions"]["deny"] == ["Read"]


def test_claude_code_deny_read_diff_idempotent(tmp_home):
    settings_path = tmp_home / "settings.json"
    settings_path.write_text(
        json.dumps({"permissions": {"deny": ["Read"]}}), encoding="utf-8"
    )

    diff = claude_code.build_deny_read_diff(settings_path)
    assert diff.changed is False
    assert diff.exists_before is True


def test_claude_code_preserves_existing_fields(tmp_home):
    settings_path = tmp_home / "settings.json"
    settings_path.write_text(
        json.dumps({"permissions": {"deny": ["Bash(rm -rf /)"], "allow": ["Read(foo)"]}, "model": "opus"}),
        encoding="utf-8",
    )

    diff = claude_code.build_deny_read_diff(settings_path)
    assert diff.changed is True
    assert set(diff.after["permissions"]["deny"]) == {"Bash(rm -rf /)", "Read"}
    assert diff.after["permissions"]["allow"] == ["Read(foo)"]
    assert diff.after["model"] == "opus"


def test_claude_code_invalid_json_edge_case(tmp_home):
    settings_path = tmp_home / "settings.json"
    settings_path.write_text("{not valid json", encoding="utf-8")

    diff = claude_code.build_deny_read_diff(settings_path)
    assert diff.error is not None
    assert diff.after is None
    assert diff.changed is False
    # 파싱 안 되는 파일은 절대 손대지 않는다 — apply 를 걸어도 아무 일도 없어야 함
    assert apply_diff(diff, apply=True) is False
    assert settings_path.read_text(encoding="utf-8") == "{not valid json"


def test_claude_code_non_dict_permissions_edge_case(tmp_home):
    settings_path = tmp_home / "settings.json"
    settings_path.write_text(json.dumps({"permissions": "not-a-dict"}), encoding="utf-8")

    diff = claude_code.build_deny_read_diff(settings_path)
    assert diff.error is not None
    assert diff.after is None


def test_claude_code_pretooluse_hook_diff(tmp_home):
    settings_path = tmp_home / "settings.json"
    diff = claude_code.build_pretooluse_hook_diff(settings_path)
    assert diff.changed is True
    hooks = diff.after["hooks"]["PreToolUse"]
    assert any(h.get("matcher") == "Read" for h in hooks)

    # 두 번째 호출은 이중 등록하지 않아야 함(멱등)
    applied = apply_diff(diff, apply=True)
    assert applied is True
    diff2 = claude_code.build_pretooluse_hook_diff(settings_path)
    assert diff2.changed is False


# ── cursor / windsurf ────────────────────────────────────────────────────────


def test_cursor_hooks_diff_registers_both_hooks(tmp_home):
    hooks_path = tmp_home / "cursor_hooks.json"
    diff = cursor.build_hooks_diff(hooks_path)
    assert diff.changed is True
    assert "beforeReadFile" in diff.after["hooks"]
    assert "beforeMCPExecution" in diff.after["hooks"]
    assert not hooks_path.exists()  # dry-run


def test_cursor_hooks_diff_idempotent_after_apply(tmp_home):
    hooks_path = tmp_home / "cursor_hooks.json"
    diff = cursor.build_hooks_diff(hooks_path)
    assert apply_diff(diff, apply=True) is True

    diff2 = cursor.build_hooks_diff(hooks_path)
    assert diff2.changed is False


def test_windsurf_hooks_diff_registers_both_hooks(tmp_home):
    hooks_path = tmp_home / "windsurf_hooks.json"
    diff = windsurf.build_hooks_diff(hooks_path)
    assert diff.changed is True
    assert "pre_read_code" in diff.after["hooks"]
    assert "pre_mcp_tool_use" in diff.after["hooks"]


# ── cline (OS 분기) ───────────────────────────────────────────────────────────


def test_cline_macos_linux_gets_hook_diff(tmp_home):
    settings_path = tmp_home / "cline_settings.json"
    action = cline.build_action(settings_path, os_name="Darwin")
    assert hasattr(action, "changed")  # SettingsDiff
    assert action.changed is True
    assert action.after["hooks"]["PreToolUse"]

    action_linux = cline.build_action(settings_path, os_name="Linux")
    assert hasattr(action_linux, "changed")


def test_cline_windows_gets_manual_notice(tmp_home):
    settings_path = tmp_home / "cline_settings.json"
    history_path = tmp_home / "api_conversation_history.json"
    action = cline.build_action(settings_path, os_name="Windows", history_path=history_path)
    assert isinstance(action, dict)
    assert action["supported"] is False
    assert action["logPath"] == str(history_path)
    assert len(action["manualChecklist"]) >= 1
    # Windows 경로에서는 settings_path 를 절대 건드리지 않아야 함
    assert not settings_path.exists()


# ── vscode_copilot ───────────────────────────────────────────────────────────


def test_vscode_copilot_build_action_has_hook_and_manual_checklist(tmp_home):
    settings_path = tmp_home / "vscode_settings.json"
    log_path = tmp_home / "chatSessions"
    action = vscode_copilot.build_action(settings_path, log_glob_path=log_path)
    assert action["hookDiff"]["changed"] is True
    assert action["logPath"] == str(log_path)
    assert len(action["manualChecklist"]) >= 1


# ── claude_desktop (읽기 전용 감지) ───────────────────────────────────────────


def test_claude_desktop_no_file_no_warning(tmp_home):
    config_path = tmp_home / "claude_desktop_config.json"
    result = claude_desktop.detect_filesystem_servers(config_path)
    assert result["exists"] is False
    assert result["warning"] is None


def test_claude_desktop_detects_filesystem_server(tmp_home):
    config_path = tmp_home / "claude_desktop_config.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "filesystem": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me"],
                    },
                    "campfire": {"command": "npx", "args": ["campfire-mcp", "connect"]},
                }
            }
        ),
        encoding="utf-8",
    )
    before_text = config_path.read_text(encoding="utf-8")

    result = claude_desktop.detect_filesystem_servers(config_path)
    assert "filesystem" in result["servers"]
    assert "campfire" not in result["servers"]
    assert result["warning"] is not None

    # 읽기 전용 — 파일 내용이 절대 바뀌지 않아야 한다
    assert config_path.read_text(encoding="utf-8") == before_text


def test_claude_desktop_no_filesystem_server_no_warning(tmp_home):
    config_path = tmp_home / "claude_desktop_config.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"campfire": {"command": "npx", "args": ["campfire-mcp"]}}}),
        encoding="utf-8",
    )
    result = claude_desktop.detect_filesystem_servers(config_path)
    assert result["servers"] == []
    assert result["warning"] is None


def test_claude_desktop_invalid_json_edge_case(tmp_home):
    config_path = tmp_home / "claude_desktop_config.json"
    config_path.write_text("not json at all", encoding="utf-8")
    result = claude_desktop.detect_filesystem_servers(config_path)
    assert result["error"] is not None
    assert result["servers"] == []


# ── checklist.py 종합 보고서 + CLI ────────────────────────────────────────────


def test_build_report_covers_all_seven_clients(tmp_home):
    report = checklist.build_report(target_dir=tmp_home, os_name="Windows")
    clients = report["clients"]
    assert set(clients.keys()) == {
        "claude_code", "cursor", "windsurf", "cline", "vscode_copilot", "claude_desktop",
    }
    # Windows 에서는 cline 이 수동 체크리스트 경로를 타야 한다
    assert clients["cline"]["status"] == "manual"
    assert clients["cline"]["action"]["supported"] is False
    # 나머지 자동 등록 가능한 클라이언트들은 diff 를 만들었어야 한다
    assert clients["claude_code"]["denyReadDiff"]["changed"] is True
    assert clients["cursor"]["hookDiff"]["changed"] is True
    assert clients["windsurf"]["hookDiff"]["changed"] is True

    # target_dir 아래에 어떤 파일도 실제로 쓰이지 않아야 한다(dry-run 전용)
    assert list(tmp_home.iterdir()) == []


def test_build_report_macos_cline_is_auto(tmp_home):
    report = checklist.build_report(target_dir=tmp_home, os_name="Darwin")
    assert report["clients"]["cline"]["status"] == "auto"


def test_cli_dry_run_json_output(tmp_home, capsys):
    exit_code = checklist.main(["--target-dir", str(tmp_home), "--json"])
    assert exit_code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "clients" in parsed
    # dry-run 이므로 --json 만으로는 아무 파일도 쓰이지 않아야 한다
    assert list(tmp_home.iterdir()) == []


def test_cli_apply_without_target_dir_is_blocked(capsys):
    """--target-dir 없이 --apply 를 주면 build_report() 조차 호출되지 않고 즉시
    거부돼야 한다 — 실제 홈 디렉토리를 읽지도 쓰지도 않음을 보장하는 안전장치."""
    exit_code = checklist.main(["--apply"])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "안전 차단" in err


def test_cli_apply_with_target_dir_writes_only_inside_it(tmp_home, capsys):
    exit_code = checklist.main(["--apply", "--target-dir", str(tmp_home)])
    assert exit_code == 0
    written_path = tmp_home / "claude_code_settings.json"
    assert written_path.exists()
    data = json.loads(written_path.read_text(encoding="utf-8"))
    assert data["permissions"]["deny"] == ["Read"]
