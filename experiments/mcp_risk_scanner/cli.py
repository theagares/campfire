"""Local-only command line interface for the independent prototype."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .core import add_runtime_audit, assess, compare_baseline, make_baseline
from .llm import review_with_solar
from .probe import probe_loopback, validate_loopback_url
from .proxy import run_stdio_proxy


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Campfire experimental MCP risk scanner (not installed in app)")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="scan a JSON tools/list snapshot without running its server")
    scan.add_argument("snapshot", type=Path)
    scan.add_argument("--server-id", required=True)
    scan.add_argument("--source", type=Path)
    scan.add_argument("--baseline", type=Path, help="compare against approved baseline")
    scan.add_argument("--runtime-audit", type=Path, help="include redacted proxy JSONL observations")
    scan.add_argument("--approve-baseline", type=Path, help="explicitly save current approved fingerprints")
    scan.add_argument("--llm", action="store_true", help="send redacted tool definitions to Upstage Solar")
    scan.add_argument("--consent-cloud", action="store_true", help="confirm cloud transfer for --llm")

    probe = commands.add_parser("probe", help="inspect an already running numeric-loopback MCP endpoint")
    probe.add_argument("url")
    probe.add_argument("--source", type=Path)
    probe.add_argument("--baseline", type=Path)
    probe.add_argument("--runtime-audit", type=Path)
    probe.add_argument("--approve-baseline", type=Path)
    probe.add_argument("--confirm-connect", action="store_true", help="confirm target contact")

    gateway = commands.add_parser("proxy", help="start a guarded stdio proxy to an approved loopback MCP")
    gateway.add_argument("url")
    gateway.add_argument("--baseline", required=True, type=Path)
    gateway.add_argument("--audit-file", type=Path, help="append redacted call decisions as JSONL")
    gateway.add_argument("--confirm-connect", action="store_true", help="confirm target contact")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command in {"probe", "proxy"}:
        validate_loopback_url(args.url)
        if not args.confirm_connect:
            raise ValueError("--confirm-connect is required; MCP initialization contacts the target")
    if args.command == "proxy":
        await run_stdio_proxy(args.url, args.baseline, args.audit_file)
        return 0
    if args.command == "scan":
        tools = json.loads(args.snapshot.read_text(encoding="utf-8"))
        if not isinstance(tools, list):
            raise ValueError("snapshot must be a JSON array of MCP tool definitions")
        server_id = args.server_id
    else:
        tools = await probe_loopback(args.url)
        server_id = args.url
    report = assess(server_id, tools, source=args.source)
    if args.runtime_audit:
        if args.runtime_audit.stat().st_size > 1_000_000:
            raise ValueError("runtime audit exceeds 1 MB")
        events = [json.loads(line) for line in args.runtime_audit.read_text(encoding="utf-8").splitlines() if line.strip()]
        add_runtime_audit(report, events)
    if args.command == "scan" and args.llm:
        report.llm_suggestions = await review_with_solar(tools, consent=args.consent_cloud)
    output = report.to_dict()
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        changes = compare_baseline(report, baseline)
        output["baselineChanges"] = changes
        if changes:
            output["verdict"] = "definition_changed"
    if args.approve_baseline:
        if output["verdict"] in {"critical", "definition_changed"}:
            raise ValueError("cannot approve a critical or changed catalog")
        args.approve_baseline.write_text(json.dumps(make_baseline(report), indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (ValueError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"scanner error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
