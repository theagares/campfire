"""Evidence-based MCP risk assessment; no server installation or execution."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator


# Penalty budgets. A point means observed risk, not a probability of compromise.
CAPS = {
    "tool_poisoning": 20,
    "permission_scope": 15,
    "file_system_access": 15,
    "network_access": 10,
    "secret_access": 15,
    "dependency_risk": 10,
    "prompt_injection": 10,
    "runtime_behavior": 5,
}
MAX_SOURCE_FILES = 200
MAX_SOURCE_BYTES = 2_000_000
MAX_SOURCE_ENTRIES = 5_000
MAX_MANIFEST_BYTES = 1_000_000
MAX_TOOL_COUNT = 200
MAX_DEFINITION_BYTES = 1_000_000
MAX_BASELINE_BYTES = 256_000
MAX_INTEGRITY_KEY_BYTES = 4_096
MAX_AUDIT_BYTES = 1_000_000
MAX_RUNTIME_INSPECTION_BYTES = 64_000
MAX_RUNTIME_STRING_FIELDS = 1_000
MAX_RUNTIME_NODES = 5_000
INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
PRIVATE_PATH = re.compile(r"(?:~/\.ssh/|[/\\]\.ssh[/\\]|\.env\b|id_rsa\b|credentials\b|secrets?\b)", re.I)
EXFIL_VERB = re.compile(r"\b(?:send|upload|post|exfiltrate|include|transmit|전송|업로드|포함)\b", re.I)
ROLE_OVERRIDE = re.compile(r"(?:ignore (?:all |the )?(?:previous|prior) instructions|you are now (?:the )?system|<\s*/?system\s*>|developer message:|system prompt:|이전 지시(?:를|사항을)? 무시)", re.I)
EXTERNAL_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
SENSITIVE_VALUE = re.compile(r"(?:-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b)")


@dataclass(frozen=True)
class Finding:
    code: str
    category: str
    points: int
    severity: str
    evidence: str
    message: str
    confidence: str = "observed"


@dataclass
class Report:
    server_id: str
    findings: list[Finding] = field(default_factory=list)
    coverage: dict[str, str] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    llm_suggestions: list[dict[str, str]] = field(default_factory=list)

    def add(self, code: str, category: str, points: int, severity: str, evidence: str,
            message: str, confidence: str = "observed") -> None:
        if category not in CAPS or points < 0:
            raise ValueError("invalid finding")
        finding = Finding(code, category, points, severity, evidence, message, confidence)
        if finding not in self.findings:
            self.findings.append(finding)

    @property
    def penalties(self) -> dict[str, int]:
        return {category: min(cap, sum(f.points for f in self.findings if f.category == category))
                for category, cap in CAPS.items()}

    @property
    def risk_score(self) -> int:
        return sum(self.penalties.values())

    @property
    def security_score(self) -> int:
        return 100 - self.risk_score

    @property
    def verdict(self) -> str:
        if any(f.severity == "critical" for f in self.findings):
            return "critical"
        if self.coverage.get("tool_definitions") != "checked":
            return "insufficient_evidence"
        if self.risk_score >= 40:
            return "high_risk"
        if self.risk_score >= 20 or self.llm_suggestions:
            return "review"
        if (self.coverage.get("source_code") != "checked"
                or self.coverage.get("dependencies") != "checked_manifest_only"
                or self.coverage.get("runtime") != "checked_mcp_messages"):
            return "limited_visibility"
        return "low_observed_risk"

    def to_dict(self) -> dict[str, Any]:
        return {
            "serverId": self.server_id,
            "riskScore": self.risk_score,
            "securityScore": self.security_score,
            "verdict": self.verdict,
            "penalties": {k: {"points": v, "max": CAPS[k]} for k, v in self.penalties.items()},
            "coverage": self.coverage,
            "findings": [asdict(f) for f in self.findings],
            "fingerprints": self.fingerprints,
            "llmSuggestions": self.llm_suggestions,
        }


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def fingerprint(tool: dict[str, Any]) -> str:
    """Bind every exposed tool field, including annotations and metadata."""
    return hashlib.sha256(_canonical(tool)).hexdigest()


def _strings(value: Any, path: str = "", *, max_nodes: int | None = None,
             limit_reached: list[bool] | None = None) -> Iterator[tuple[str, str]]:
    """Yield nested strings iteratively so hostile depth cannot exhaust recursion."""
    def dict_children(base: str, mapping: dict[Any, Any]) -> Iterator[tuple[str, Any]]:
        for key, item in mapping.items():
            yield (f"{base}.{key}" if base else str(key), item)

    def list_children(base: str, values: list[Any]) -> Iterator[tuple[str, Any]]:
        for index, item in enumerate(values):
            yield f"{base}[{index}]", item

    stack: list[Iterator[tuple[str, Any]]] = [iter(((path, value),))]
    nodes = 0
    while stack:
        try:
            current_path, current = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        nodes += 1
        if max_nodes is not None and nodes > max_nodes:
            if limit_reached is not None:
                limit_reached[0] = True
            return
        if isinstance(current, str):
            yield current_path, current
        elif isinstance(current, dict):
            stack.append(dict_children(current_path, current))
        elif isinstance(current, list):
            stack.append(list_children(current_path, current))


def _utf8_prefix(value: str, byte_budget: int) -> tuple[str, int, bool]:
    """Return a UTF-8-safe prefix without allocating an encoded copy of all input."""
    if byte_budget <= 0:
        return "", 0, bool(value)
    chunks: list[str] = []
    used = 0
    position = 0
    while position < len(value) and used < byte_budget:
        chunk = value[position:position + min(4096, byte_budget - used)]
        encoded = chunk.encode("utf-8")
        remaining = byte_budget - used
        if len(encoded) <= remaining:
            chunks.append(chunk)
            used += len(encoded)
            position += len(chunk)
            continue
        prefix = encoded[:remaining].decode("utf-8", errors="ignore")
        chunks.append(prefix)
        used += len(prefix.encode("utf-8"))
        position += len(prefix)
        break
    return "".join(chunks), used, position < len(value)


def _scan_tool(report: Report, tool: dict[str, Any]) -> None:
    name = tool["name"]
    for field_path, value in _strings(tool):
        # A payload may appear in property descriptions/defaults/_meta, not only description.
        evidence = f"tools.{name}.{field_path}"
        if INVISIBLE.search(value):
            report.add("hidden_unicode", "tool_poisoning", 10, "warning", evidence,
                       "Invisible formatting character in tool definition")
        if PRIVATE_PATH.search(value) and EXFIL_VERB.search(value):
            report.add("credential_exfil_instruction", "tool_poisoning", 20, "critical", evidence,
                       "Instruction combines a sensitive path with data transfer")
        elif ROLE_OVERRIDE.search(value):
            report.add("role_override", "prompt_injection", 10, "critical", evidence,
                       "Tool definition contains an instruction/role override")

    lower_name = name.lower()
    if re.search(r"(?:execute|shell|terminal|run_command|delete|remove|write_file)", lower_name):
        report.add("privileged_tool", "permission_scope", 5, "warning", f"tools.{name}.name",
                   "Tool name suggests a privileged action", "inferred")
    if re.search(r"(?:read_file|write_file|list_files|search_files|filesystem)", lower_name):
        report.add("filesystem_tool", "file_system_access", 4, "info", f"tools.{name}.name",
                   "Tool appears to have filesystem access", "inferred")
    if re.search(r"(?:fetch_url|http_request|send_email|post_data|webhook)", lower_name):
        report.add("network_tool", "network_access", 3, "info", f"tools.{name}.name",
                   "Tool appears to reach the network", "inferred")
    if re.search(r"(?:get_secret|read_secret|credential|token)", lower_name):
        report.add("secret_tool", "secret_access", 5, "warning", f"tools.{name}.name",
                   "Tool appears to access credentials", "inferred")
    annotations = tool.get("annotations") or {}
    if not isinstance(annotations, dict):
        raise ValueError("tool annotations must be an object")
    if annotations.get("readOnlyHint") is True and re.search(r"(?:delete|remove|write|send|post|execute)", lower_name):
        report.add("contradictory_annotation", "permission_scope", 7, "warning",
                   f"tools.{name}.annotations.readOnlyHint", "Read-only claim conflicts with tool name")
    schema = tool.get("inputSchema") or {}
    if not isinstance(schema, dict):
        raise ValueError("tool inputSchema must be an object")
    if schema.get("additionalProperties") is True and re.search(r"(?:execute|shell|http|request)", lower_name):
        report.add("permissive_schema", "permission_scope", 4, "warning",
                   f"tools.{name}.inputSchema.additionalProperties", "Privileged tool accepts undeclared fields")


def _scan_source(report: Report, source: Path) -> None:
    if not source.is_dir():
        raise ValueError("source must be an existing directory")
    root = source.resolve()
    total = 0
    files = 0
    entries = 0
    source_patterns = [
        (re.compile(r"(?:\.ssh[/\\]|id_rsa\b|\.env\b)"), "sensitive_path", "secret_access", 8,
         "Source references sensitive local paths"),
        (re.compile(r"(?:os\.environ|os\.getenv|process\.env)"), "environment_access", "secret_access", 4,
         "Source reads environment variables"),
        (re.compile(r"(?:subprocess\.|child_process|exec\(|spawn\()"), "command_execution", "permission_scope", 7,
         "Source can launch commands"),
        (re.compile(r"(?:requests\.post|httpx\.post|fetch\(|axios\.post)"), "outbound_network", "network_access", 5,
         "Source can issue outbound network requests"),
        (re.compile(r"(?:open\(|readFile|writeFile|fs\.read|fs\.write)"), "filesystem_code", "file_system_access", 4,
         "Source can read or write files"),
    ]
    supported = {".py", ".js", ".ts", ".mjs", ".cjs"}
    excluded = {"node_modules", ".git", "venv", ".venv"}
    report.coverage["source_scope"] = "py,js,ts,mjs,cjs heuristic patterns"
    directories = [root]
    while directories:
        directory = directories.pop()
        child_directories: list[Path] = []
        source_files: list[Path] = []
        with os.scandir(directory) as directory_entries:
            for entry in directory_entries:
                entries += 1
                if entries > MAX_SOURCE_ENTRIES:
                    report.coverage["source_code"] = "partial_limit_reached"
                    return
                if entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False) and entry.name not in excluded:
                    child_directories.append(path)
                elif entry.is_file(follow_symlinks=False) and path.suffix.lower() in supported:
                    source_files.append(path)
        directories.extend(reversed(sorted(child_directories)))
        for path in sorted(source_files):
            size = path.stat().st_size
            files += 1
            total += size
            if files > MAX_SOURCE_FILES or total > MAX_SOURCE_BYTES:
                report.coverage["source_code"] = "partial_limit_reached"
                return
            for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for pattern, code, category, points, message in source_patterns:
                    if pattern.search(line):
                        report.add(code, category, points, "info", f"{path.relative_to(root)}:{line_no}",
                                   message, "inferred")
    report.coverage["source_code"] = "checked"


def _scan_dependencies(report: Report, source: Path) -> None:
    root = source.resolve()
    package = root / "package.json"
    requirements = root / "requirements.txt"
    if any(path.is_symlink() for path in (package, requirements)):
        report.coverage["dependencies"] = "partial_symlink_skipped"
        return
    lock_exists = any(path.is_file() and not path.is_symlink() for path in
                      (root / name for name in
                       ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml",
                        "yarn.lock", "uv.lock", "poetry.lock")))
    seen = False
    manifest_bytes = sum(path.stat().st_size for path in (package, requirements) if path.is_file())
    if manifest_bytes > MAX_MANIFEST_BYTES:
        report.coverage["dependencies"] = "partial_limit_reached"
        return
    if package.is_file():
        seen = True
        data = json.loads(package.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("package.json must contain an object")
        scripts = data.get("scripts", {})
        if not isinstance(scripts, dict):
            raise ValueError("package.json scripts must be an object")
        for key in ("preinstall", "install", "postinstall", "prepare"):
            if key in scripts:
                report.add("install_script", "dependency_risk", 6, "warning", f"package.json:scripts.{key}",
                           "Package executes a script during installation")
        dependency_sections = []
        for section in ("dependencies", "devDependencies"):
            values = data.get(section, {})
            if not isinstance(values, dict):
                raise ValueError(f"package.json {section} must be an object")
            dependency_sections.append(values)
        if any(str(version).startswith(("*", "^", "~", ">", "<", "latest"))
               for values in dependency_sections for version in values.values()):
            report.add("floating_dependency", "dependency_risk", 3, "info", "package.json:dependencies",
                       "Dependency versions are not exact", "inferred")
    if requirements.is_file():
        seen = True
        for line_no, line in enumerate(requirements.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "==" not in stripped and " @ " not in stripped:
                report.add("floating_dependency", "dependency_risk", 3, "info",
                           f"requirements.txt:{line_no}", "Dependency is not pinned", "inferred")
    if seen and not lock_exists:
        report.add("missing_lockfile", "dependency_risk", 3, "info", str(root),
                   "No supported dependency lockfile found", "inferred")
    report.coverage["dependencies"] = "checked_manifest_only" if seen else "not_available"


def assess(server_id: str, tools: list[dict[str, Any]], source: Path | None = None,
           scopes: list[str] | None = None) -> Report:
    """Assess a supplied snapshot. Never starts or invokes the target MCP server."""
    if not isinstance(server_id, str) or not server_id or len(server_id) > 256:
        raise ValueError("invalid server ID")
    if not isinstance(tools, list):
        raise ValueError("tools must be a list")
    try:
        definition_bytes = _canonical(tools)
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ValueError("tool catalog is not valid JSON data") from exc
    if len(tools) > MAX_TOOL_COUNT or len(definition_bytes) > MAX_DEFINITION_BYTES:
        raise ValueError("tool catalog too large")
    report = Report(server_id=server_id, coverage={"tool_definitions": "checked", "source_code": "not_available",
                                                    "dependencies": "not_available", "runtime": "not_checked"})
    names: set[str] = set()
    for tool in tools:
        if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                or not tool["name"] or len(tool["name"]) > 256):
            raise ValueError("invalid tool definition")
        if tool["name"] in names:
            raise ValueError("duplicate tool name")
        names.add(tool["name"])
        report.fingerprints[tool["name"]] = fingerprint(tool)
        _scan_tool(report, tool)
    if scopes is not None and (not isinstance(scopes, list) or len(scopes) > 100
                               or any(not isinstance(scope, str) or len(scope) > 256
                                      for scope in scopes)):
        raise ValueError("invalid authorization scopes")
    for scope in scopes or []:
        if re.search(r"(?:admin|full|write|delete|repo$|all)", scope, re.I):
            report.add("broad_scope", "permission_scope", 8, "warning", "authorization.scopes",
                       "Granted scope may permit modification", "inferred")
            break
    if source is not None:
        _scan_source(report, source)
        _scan_dependencies(report, source)
    return report


def make_baseline(report: Report) -> dict[str, Any]:
    """A reference snapshot for drift detection, not an approval or safety claim."""
    return {"format": 1, "serverId": report.server_id, "fingerprints": dict(report.fingerprints)}


def _validate_integrity_key(integrity_key: bytes) -> None:
    if len(integrity_key) < 32:
        raise ValueError("baseline integrity key must be at least 32 bytes")


def make_signed_baseline(report: Report, integrity_key: bytes) -> dict[str, Any]:
    """Create an HMAC-authenticated reference; keep its key outside the baseline."""
    _validate_integrity_key(integrity_key)
    baseline: dict[str, Any] = {
        "format": 2,
        "serverId": report.server_id,
        "fingerprints": dict(report.fingerprints),
    }
    digest = hmac.new(integrity_key, _canonical(baseline), hashlib.sha256).hexdigest()
    baseline["integrity"] = {"algorithm": "hmac-sha256", "digest": digest}
    return baseline


def load_integrity_key(path: Path, *, create: bool = False) -> bytes:
    """Read a binary HMAC key, or create a private 32-byte key without overwriting."""
    if create and not path.exists():
        key = secrets.token_bytes(32)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
    try:
        if path.stat().st_size > MAX_INTEGRITY_KEY_BYTES:
            raise ValueError("baseline integrity key file is too large")
        key = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError("baseline integrity key file does not exist") from exc
    _validate_integrity_key(key)
    return key


def verify_baseline(baseline: dict[str, Any], *, server_id: str | None = None,
                    integrity_key: bytes | None = None,
                    require_integrity: bool = False) -> dict[str, str]:
    """Validate baseline structure, identity, fingerprints, and optional HMAC."""
    if not isinstance(baseline, dict) or baseline.get("format") not in {1, 2}:
        raise ValueError("invalid baseline format")
    if not isinstance(baseline.get("serverId"), str):
        raise ValueError("invalid baseline server ID")
    if server_id is not None and baseline["serverId"] != server_id:
        raise ValueError("baseline identity mismatch")
    fingerprints = baseline.get("fingerprints")
    if (not isinstance(fingerprints, dict) or len(fingerprints) > MAX_TOOL_COUNT
            or any(not isinstance(name, str) or not name or len(name) > 256
                   or not isinstance(digest, str)
                   or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                   for name, digest in fingerprints.items())):
        raise ValueError("invalid baseline fingerprints")
    if baseline["format"] == 2:
        if integrity_key is None:
            raise ValueError("signed baseline requires --baseline-key-file")
        _validate_integrity_key(integrity_key)
        integrity = baseline.get("integrity")
        if not isinstance(integrity, dict) or integrity.get("algorithm") != "hmac-sha256":
            raise ValueError("invalid baseline integrity metadata")
        supplied = integrity.get("digest")
        payload = {key: value for key, value in baseline.items() if key != "integrity"}
        try:
            expected = hmac.new(integrity_key, _canonical(payload), hashlib.sha256).hexdigest()
        except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
            raise ValueError("invalid signed baseline data") from exc
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
            raise ValueError("baseline integrity check failed")
    elif require_integrity:
        raise ValueError("unsigned baseline is not accepted with --baseline-key-file")
    return fingerprints


def compare_baseline(report: Report, baseline: dict[str, Any], *,
                     integrity_key: bytes | None = None,
                     require_integrity: bool = False) -> list[dict[str, str]]:
    old = verify_baseline(baseline, server_id=report.server_id, integrity_key=integrity_key,
                          require_integrity=require_integrity)
    changes = []
    for name in sorted(set(old) | set(report.fingerprints)):
        before = old.get(name)
        after = report.fingerprints.get(name)
        if before != after:
            changes.append({"tool": name, "change": "added" if before is None else
                            "removed" if after is None else "modified"})
    return changes


def inspect_runtime_payload(tool_name: str, arguments: dict[str, Any] | None = None,
                            response_text: str | None = None) -> list[Finding]:
    """Inspect MCP messages visible to a proxy, not hidden OS/network activity."""
    findings: list[Finding] = []
    remaining = MAX_RUNTIME_INSPECTION_BYTES
    fields = 0
    truncated = False
    node_limit_reached = [False]
    argument_strings = iter(_strings(arguments or {}, "arguments", max_nodes=MAX_RUNTIME_NODES,
                                     limit_reached=node_limit_reached))
    for field_path, value in argument_strings:
        fields += 1
        if fields > MAX_RUNTIME_STRING_FIELDS:
            truncated = True
            break
        inspected, used, was_truncated = _utf8_prefix(value, remaining)
        remaining -= used
        truncated = truncated or was_truncated
        if SENSITIVE_VALUE.search(inspected):
            findings.append(Finding("secret_in_argument", "runtime_behavior", 5, "critical",
                                    f"{tool_name}.{field_path}", "Credential-like value in tool arguments"))
        if PRIVATE_PATH.search(inspected) and re.search(r"(?:url|query|body|message|recipient)", field_path, re.I):
            findings.append(Finding("private_path_outbound", "runtime_behavior", 5, "critical",
                                    f"{tool_name}.{field_path}", "Sensitive path in an outbound-looking argument"))
        if remaining == 0:
            if not truncated:
                try:
                    next(argument_strings)
                except StopIteration:
                    pass
                else:
                    truncated = True
            break
    truncated = truncated or node_limit_reached[0]
    if truncated:
        findings.append(Finding("uninspectable_argument", "runtime_behavior", 5, "warning",
                                f"{tool_name}.arguments", "Tool arguments exceeded inspection coverage"))
    if response_text:
        inspected, _, response_truncated = _utf8_prefix(response_text, MAX_RUNTIME_INSPECTION_BYTES)
        if ROLE_OVERRIDE.search(inspected) or (PRIVATE_PATH.search(inspected) and EXFIL_VERB.search(inspected)):
            findings.append(Finding("poisoned_tool_result", "runtime_behavior", 5, "critical",
                                    f"{tool_name}.result", "Tool result contains an instruction attack"))
        if SENSITIVE_VALUE.search(inspected):
            findings.append(Finding("secret_in_result", "runtime_behavior", 5, "critical",
                                    f"{tool_name}.result", "Credential-like value in tool result"))
        if response_truncated:
            findings.append(Finding("uninspectable_result", "runtime_behavior", 5, "warning",
                                    f"{tool_name}.result", "Tool result exceeded inspection coverage"))
    return findings


# Only scanner-generated, redacted audit codes are accepted for score updates.
RUNTIME_REASON_CODES = {
    "sensitive_argument": "Credential-like value or sensitive path appeared in tool arguments",
    "uninspectable_argument": "Tool arguments exceeded inspection coverage",
    "poisoned_result": "Tool result contained an instruction attack or credential-like value",
    "catalog_changed": "Tool definition differs from the saved reference snapshot",
    "uninspectable_result": "Tool result exceeded inspection coverage",
}


def add_runtime_audit(report: Report, events: list[dict[str, Any]]) -> None:
    """Score only observations from the MCP proxy; not hidden server OS behavior."""
    if len(events) > 10_000:
        raise ValueError("runtime audit exceeds event limit")
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError("invalid runtime event")
        if event.get("decision") not in {"listed", "forwarded"}:
            raise ValueError("invalid runtime decision")
        codes = event.get("signals", [])
        if not isinstance(codes, list) or len(codes) > len(RUNTIME_REASON_CODES):
            raise ValueError("invalid runtime signals")
        for code in codes:
            if not isinstance(code, str) or code not in RUNTIME_REASON_CODES:
                raise ValueError("unknown runtime reason code")
            tool = re.sub(r"[^A-Za-z0-9_.-]", "_", str(event.get("tool", "?"))[:128])
            report.add(code, "runtime_behavior", 5,
                       "critical" if code in {"sensitive_argument", "poisoned_result"} else "warning",
                       f"runtimeAudit[{index}].{tool}", RUNTIME_REASON_CODES[code])
    if any(event.get("decision") == "forwarded" for event in events):
        report.coverage["runtime"] = "checked_mcp_messages"
    else:
        report.coverage["runtime"] = "metadata_only_no_calls" if events else "no_messages_observed"
