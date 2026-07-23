"""
app/onboarding/checklist.py
MCP 우회 방지 온보딩 체크리스트 CLI (PLAN §4.2, §10 Phase 6 완료 기준).

    python -m app.onboarding.checklist --dry-run

7개 클라이언트(Claude Code/Cursor/Windsurf/Cline mac·linux/Cline Windows/
VS Code Copilot Chat/Claude Desktop) 를 순회하며 감지 + 상태 + 권장 조치를
종합한 보고서(JSON/텍스트)를 생성한다.

안전 수칙(필독, 이 CLI 를 실행하는 모든 사람에게 적용됨):
    - 기본은 항상 dry-run 이다 — diff 를 보여줄 뿐 어떤 파일도 쓰지 않는다.
    - --target-dir 를 넘기지 않으면 각 클라이언트의 "실제 기본 설정 경로"
      (Path.home() 하위)를 대상으로 diff 를 "생성"하지만(읽기만 함, 존재하면
      읽어서 비교), --apply 를 주지 않는 한 그 무엇도 실제로 쓰지 않는다.
    - 한 걸음 더 나아가, 이 CLI 는 구조적으로 **--target-dir 없이는 --apply 를
      거부한다** — 실제 홈 디렉토리의 설정 파일을 실수로 덮어쓸 위험을 CLI
      레벨에서 원천 차단하기 위함이다(Phase 6 안전 수칙).
    - pytest 는 이 CLI 를 실제 홈 경로로 실행하지 않는다 — 각 클라이언트 모듈의
      build_*_diff() 함수를 tempfile 임시 디렉토리 경로로 직접 호출해 검증한다.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path
from typing import Any

from . import claude_code, claude_desktop, cline, cursor, vscode_copilot, windsurf
from .common import apply_diff


def build_report(*, target_dir: Path | None = None, os_name: str | None = None) -> dict[str, Any]:
    """7개 클라이언트 전부에 대해 감지 + diff + 권장 조치를 종합한 보고서를 만든다(적용 없음).

    target_dir 를 주면(테스트/격리용) 모든 클라이언트의 대상 파일이 그 디렉토리
    아래 고정된 파일명으로 격리된다 — 실제 홈 디렉토리는 전혀 건드리지 않는다.
    target_dir 를 생략하면 각 클라이언트의 실 사용 기본 경로(Path.home() 하위)를
    대상으로 "읽어서 diff 만" 생성한다(쓰기는 --apply 없이는 절대 없음).
    """
    os_name = os_name or platform.system()

    if target_dir is not None:
        target_dir = Path(target_dir)
        claude_code_path = target_dir / "claude_code_settings.json"
        cursor_path = target_dir / "cursor_hooks.json"
        windsurf_path = target_dir / "windsurf_hooks.json"
        cline_path = target_dir / "cline_settings.json"
        vscode_path = target_dir / "vscode_settings.json"
        claude_desktop_path = target_dir / "claude_desktop_config.json"
    else:
        claude_code_path = claude_code.DEFAULT_SETTINGS_PATH
        cursor_path = cursor.DEFAULT_HOOKS_PATH
        windsurf_path = windsurf.DEFAULT_HOOKS_PATH
        cline_path = cline.DEFAULT_SETTINGS_PATH
        vscode_path = vscode_copilot.DEFAULT_SETTINGS_PATH
        claude_desktop_path = claude_desktop.DEFAULT_CONFIG_PATH

    cc_deny = claude_code.build_deny_read_diff(claude_code_path)
    cc_hook = claude_code.build_pretooluse_hook_diff(claude_code_path)
    cursor_diff = cursor.build_hooks_diff(cursor_path)
    windsurf_diff = windsurf.build_hooks_diff(windsurf_path)
    cline_action = cline.build_action(cline_path, os_name=os_name)
    vscode_action = vscode_copilot.build_action(vscode_path)
    desktop_detect = claude_desktop.detect_filesystem_servers(claude_desktop_path)

    cline_is_manual = isinstance(cline_action, dict) and cline_action.get("supported") is False
    cline_action_dict = cline_action if cline_is_manual else cline_action.to_dict()

    clients = {
        "claude_code": {
            "mechanism": "설정파일 자동 편집(permissions.deny) + PreToolUse 훅 이중 방어",
            "denyReadDiff": cc_deny.to_dict(),
            "hookDiff": cc_hook.to_dict(),
            "status": "manual" if cc_deny.error else "auto",
        },
        "cursor": {
            "mechanism": "공식 Hooks 자동 등록(beforeReadFile/beforeMCPExecution)",
            "hookDiff": cursor_diff.to_dict(),
            "status": "manual" if cursor_diff.error else "auto",
        },
        "windsurf": {
            "mechanism": "공식 Cascade Hooks 자동 등록(pre_read_code/pre_mcp_tool_use)",
            "hookDiff": windsurf_diff.to_dict(),
            "status": "manual" if windsurf_diff.error else "auto",
        },
        "cline": {
            "mechanism": "macOS/Linux: PreToolUse 훅 자동 등록 / Windows: 로그 파싱 + 수동 체크리스트",
            "action": cline_action_dict,
            "status": "manual" if cline_is_manual else "auto",
        },
        "vscode_copilot": {
            "mechanism": "Agent Hooks(Preview) 등록 시도 + 로그 파싱 병행 + 수동 체크리스트",
            "action": vscode_action,
            "status": "manual",  # Preview 라 완전 자동으로 신뢰하지 않음(PLAN §4.2)
        },
        "claude_desktop": {
            "mechanism": "claude_desktop_config.json mcpServers 읽기 전용 감지(수정 안 함)",
            "detection": desktop_detect,
            "status": "warning" if desktop_detect.get("warning") else "n/a",
        },
    }
    return {"osName": os_name, "clients": clients}


def _print_text_report(report: dict[str, Any]) -> None:
    print(f"MCP 우회 방지 온보딩 체크리스트 (OS: {report['osName']})")
    print("=" * 64)
    for client_name, info in report["clients"].items():
        print(f"\n[{client_name}]")
        print(f"  mechanism : {info['mechanism']}")
        print(f"  status    : {info['status']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.onboarding.checklist",
        description="securedoc-gateway MCP 우회 방지 온보딩 체크리스트(PLAN §4.2)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="(기본값, 항상 켜짐) diff 만 생성하고 적용하지 않음",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="실제로 설정 파일에 적용(반드시 --target-dir 와 함께 사용해야 함)",
    )
    parser.add_argument(
        "--target-dir", type=str, default=None,
        help="대상 디렉토리 override(테스트/격리용) — 지정하지 않으면 실제 홈 디렉토리 기본 경로를 읽기 전용으로 조회만 함",
    )
    parser.add_argument("--json", action="store_true", help="JSON 형식으로 출력")
    args = parser.parse_args(argv)

    target_dir = Path(args.target_dir) if args.target_dir else None

    # 안전 차단은 build_report() 호출(= 실제 대상 경로 조회) 전에 수행한다 —
    # --target-dir 없이 --apply 가 오면 실제 홈 디렉토리를 단 한 번도 건드리지
    # 않고(읽기조차 하지 않고) 즉시 거부한다.
    if args.apply and target_dir is None:
        print(
            "[안전 차단] --target-dir 없이 --apply 는 실제 홈 디렉토리 설정을 "
            "건드릴 위험이 있어 허용하지 않습니다. --target-dir 를 지정한 뒤 다시 시도하세요.",
            file=sys.stderr,
        )
        return 1

    report = build_report(target_dir=target_dir)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_text_report(report)

    if args.apply:
        applied: list[str] = []
        cc_path = target_dir / "claude_code_settings.json"
        if apply_diff(claude_code.build_deny_read_diff(cc_path), apply=True):
            applied.append(str(cc_path))
        print(f"\n적용됨: {applied}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
