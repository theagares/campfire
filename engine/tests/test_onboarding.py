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


def test_claude_code_does_not_emit_phantom_hook(tmp_home):
    """PreToolUse "이중 방어" 훅은 더 이상 심지 않는다.

    그 훅의 command 는 campfire-block-read 였는데 그런 스크립트는 저장소에도 배포
    패키지에도 없다 — 이중 방어가 아니라 실행되지 않는 훅이 하나 더 박히는 것뿐이었고,
    사용자는 두 겹으로 막혔다고 믿게 된다. permissions.deny 는 외부 스크립트 없이
    동작하므로 그대로 남는다."""
    assert not hasattr(claude_code, "build_pretooluse_hook_diff")

    settings_path = tmp_home / "settings.json"
    diff = claude_code.build_deny_read_diff(settings_path)
    assert diff.changed is True
    assert diff.after["permissions"]["deny"] == ["Read"]
    assert "hooks" not in diff.after


# ── cursor / windsurf ────────────────────────────────────────────────────────


@pytest.mark.parametrize("mod, name", [(cursor, "cursor"), (windsurf, "windsurf")])
def test_unconfirmed_spec_clients_return_manual_notice(mod, name, tmp_home):
    """스펙이 확정되지 않은 클라이언트에는 아무것도 쓰지 않는다.

    예전에는 추정 스키마로 훅을 써 넣었고 그 command 는 실재하지 않는
    campfire-block-read 였다. 막지도 못하면서 "등록됨" 으로 보고돼, 사용자가 우회를
    막았다고 믿는 상태가 된다 — 보안 제품에서 가장 나쁜 실패다."""
    hooks_path = tmp_home / (name + "_hooks.json")
    action = mod.build_action(hooks_path)

    assert action["supported"] is False
    assert action["reason"]
    assert len(action["manualChecklist"]) >= 1
    assert not hasattr(mod, "build_hooks_diff"), "자동 등록 경로가 되살아났다"
    assert not hooks_path.exists(), "설정 파일을 건드리면 안 된다"


# ── cline (OS 분기) ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
def test_cline_macos_linux_is_manual_too(os_name, tmp_home):
    """훅 커맨드 스펙과 배포용 차단 스크립트가 없으므로 macOS/Linux 도 수동이다."""
    settings_path = tmp_home / "cline_settings.json"
    action = cline.build_action(settings_path, os_name=os_name)
    assert action["supported"] is False
    assert len(action["manualChecklist"]) >= 1
    assert not settings_path.exists()


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


def test_vscode_copilot_is_manual_with_log_path(tmp_home):
    """Agent Hooks 는 Preview 라 추정 스키마로 심지 않는다 — 로그 점검 + 수동 안내만."""
    settings_path = tmp_home / "vscode_settings.json"
    log_path = tmp_home / "chatSessions"
    action = vscode_copilot.build_action(settings_path, log_glob_path=log_path)
    assert action["supported"] is False
    assert action["logPath"] == str(log_path)
    assert len(action["manualChecklist"]) >= 1
    assert not settings_path.exists()


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
    # 자동 조치가 가능한 것은 Claude Code 의 permissions.deny 하나뿐이다.
    assert clients["claude_code"]["denyReadDiff"]["changed"] is True
    assert clients["claude_code"]["status"] == "auto"
    for name in ("cursor", "windsurf", "vscode_copilot"):
        assert clients[name]["status"] == "manual", name
        assert clients[name]["action"]["supported"] is False, name

    # target_dir 아래에 어떤 파일도 실제로 쓰이지 않아야 한다(dry-run 전용)
    assert list(tmp_home.iterdir()) == []


def test_build_report_macos_cline_is_manual(tmp_home):
    """예전엔 macOS 에서 cline 이 auto 였다 — 실행되지 않는 훅을 심는 auto 였다."""
    report = checklist.build_report(target_dir=tmp_home, os_name="Darwin")
    assert report["clients"]["cline"]["status"] == "manual"


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


def test_apply_never_writes_the_phantom_block_read_command(tmp_home):
    """--apply 가 쓴 어떤 파일에도 campfire-block-read 가 들어가면 안 된다.

    이 이름의 스크립트는 저장소에도 배포 패키지에도 없다. 예전에는 5개 클라이언트가
    이 command 로 훅을 등록했고, 적용하면 사용자 설정에 실행 불가능한 훅이 박혔다.
    차단은 안 되는데 보고서에는 "등록됨" 으로 나온다.

    이 테스트는 그 command 가 어떤 경로로든 다시 들어오면 깨진다."""
    assert checklist.main(["--apply", "--target-dir", str(tmp_home)]) == 0

    written = sorted(x.name for x in tmp_home.iterdir())
    assert written == ["claude_code_settings.json"], "예상 밖의 파일을 썼다: " + str(written)

    for path in tmp_home.iterdir():
        body = path.read_text(encoding="utf-8")
        assert "campfire-block-read" not in body, path.name + " 에 유령 커맨드가 들어갔다"
