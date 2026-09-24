"""Local-only command line interface for the independent prototype."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from .core import (
    MAX_BASELINE_BYTES,
    MAX_DEFINITION_BYTES,
    MAX_AUDIT_BYTES,
    add_runtime_audit,
    assess,
    compare_baseline,
    load_integrity_key,
    make_baseline,
    make_signed_baseline,
)
from .llm import review_with_solar
from .probe import probe_loopback, validate_loopback_url
from .proxy import run_stdio_proxy
from .reporting import render_text
from .server import run_scanner_stdio


def _read_json_file(path: Path, *, maximum: int, label: str):
    if path.stat().st_size > maximum:
        raise ValueError(f"{label} exceeds {maximum} bytes")
    return json.loads(path.read_text(encoding="utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Campfire detachable MCP risk scanner")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="scan a JSON tools/list snapshot without running its server")
    scan.add_argument("snapshot", type=Path)
    scan.add_argument("--server-id", required=True)
    scan.add_argument("--source", type=Path)
    scan.add_argument("--scope", action="append", help="declared authorization scope; repeat as needed")
    scan.add_argument("--baseline", type=Path, help="compare against a saved reference snapshot")
    scan.add_argument("--baseline-key-file", type=Path, help="verify or create an HMAC-signed baseline")
    scan.add_argument("--runtime-audit", type=Path, help="include redacted proxy JSONL observations")
    scan.add_argument("--save-baseline", type=Path, help="save current fingerprints as a reference snapshot")
    scan.add_argument("--json", action="store_true", help="print machine-readable JSON instead of a text report")
    scan.add_argument("--llm", action="store_true", help="send redacted tool definitions to Upstage Solar")
    scan.add_argument("--consent-cloud", action="store_true", help="confirm cloud transfer for --llm")

    probe = commands.add_parser("probe", help="inspect an already running numeric-loopback MCP endpoint")
    probe.add_argument("url")
    probe.add_argument("--source", type=Path)
    probe.add_argument("--scope", action="append", help="declared authorization scope; repeat as needed")
    probe.add_argument("--baseline", type=Path)
    probe.add_argument("--baseline-key-file", type=Path, help="verify or create an HMAC-signed baseline")
    probe.add_argument("--runtime-audit", type=Path)
    probe.add_argument("--save-baseline", type=Path)
    probe.add_argument("--json", action="store_true")
    probe.add_argument("--llm", action="store_true", help="send redacted tool definitions to Upstage Solar")
    probe.add_argument("--consent-cloud", action="store_true", help="confirm cloud transfer for --llm")
    probe.add_argument("--confirm-connect", action="store_true", help="confirm target contact")

    gateway = commands.add_parser("proxy", help="start a non-blocking stdio observer for a loopback MCP")
    gateway.add_argument("url")
    gateway.add_argument("--baseline", type=Path, help="optional saved fingerprint reference")
    gateway.add_argument("--baseline-key-file", type=Path, help="verify an HMAC-signed baseline")
    gateway.add_argument("--audit-file", type=Path, help="append redacted observations as JSONL")
    gateway.add_argument("--confirm-connect", action="store_true", help="confirm target contact")
    service = commands.add_parser("serve", help="run the risk scanner itself as a local stdio MCP server")
    service.add_argument("--allow-loopback-probe", action="store_true",
                         help="allow MCP callers to contact confirmed numeric-loopback targets")
    service.add_argument("--allow-cloud-llm", action="store_true",
                         help="allow explicitly confirmed Solar cloud review calls")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "serve":
        await run_scanner_stdio(allow_loopback_probe=args.allow_loopback_probe,
                                allow_cloud_llm=args.allow_cloud_llm)
        return 0
    if args.command in {"probe", "proxy"}:
        validate_loopback_url(args.url)
        if not args.confirm_connect:
            raise ValueError("--confirm-connect is required; MCP initialization contacts the target")
    if args.command == "proxy":
        await run_stdio_proxy(args.url, args.baseline, args.audit_file, args.baseline_key_file)
        return 0
    if args.command == "scan":
        tools = _read_json_file(args.snapshot, maximum=MAX_DEFINITION_BYTES, label="snapshot")
        if not isinstance(tools, list):
            raise ValueError("snapshot must be a JSON array of MCP tool definitions")
        server_id = args.server_id
    else:
        tools = await probe_loopback(args.url)
        server_id = args.url
    report = assess(server_id, tools, source=args.source, scopes=args.scope)
    if args.baseline_key_file and not (args.baseline or args.save_baseline):
        raise ValueError("--baseline-key-file requires --baseline or --save-baseline")
    if (args.baseline_key_file and args.save_baseline
            and args.baseline_key_file.resolve() == args.save_baseline.resolve()):
        raise ValueError("baseline and integrity key must use different files")
    integrity_key = None
    if args.baseline_key_file:
        integrity_key = load_integrity_key(
            args.baseline_key_file,
            create=args.save_baseline is not None and args.baseline is None,
        )
    if args.runtime_audit:
        if args.runtime_audit.stat().st_size > MAX_AUDIT_BYTES:
            raise ValueError("runtime audit exceeds 1 MB")
        events = [json.loads(line) for line in args.runtime_audit.read_text(encoding="utf-8").splitlines() if line.strip()]
        add_runtime_audit(report, events)
    if args.llm:
        report.llm_suggestions = await review_with_solar(tools, consent=args.consent_cloud)
    changes: list[dict[str, str]] = []
    if args.baseline:
        baseline = _read_json_file(args.baseline, maximum=MAX_BASELINE_BYTES, label="baseline")
        changes = compare_baseline(report, baseline, integrity_key=integrity_key,
                                   require_integrity=integrity_key is not None)
        for change in changes:
            report.add("definition_changed", "runtime_behavior", 5, "warning",
                       f"tools.{change['tool']}", f"Tool definition {change['change']} since reference snapshot")
    if args.save_baseline:
        baseline = (make_signed_baseline(report, integrity_key) if integrity_key is not None
                    else make_baseline(report))
        args.save_baseline.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    if args.json:
        output = report.to_dict()
        output["baselineChanges"] = changes
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(render_text(report, changes))
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows pipes may report cp949 even when their consumer decodes UTF-8.
    # Keep a real terminal's chosen encoding, but make redirected reports portable.
    if not sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (ValueError, OSError, json.JSONDecodeError, TimeoutError, httpx.HTTPError,
            UnicodeError, RecursionError) as exc:
        print(f"scanner error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
