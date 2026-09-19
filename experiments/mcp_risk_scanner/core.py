"""Evidence-based MCP risk assessment; no server installation or execution."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


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
MAX_TOOL_COUNT = 200
MAX_DEFINITION_BYTES = 1_000_000
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
        if self.coverage.get("source_code") != "checked" or self.coverage.get("runtime") != "checked":
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


def _strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        result: list[tuple[str, str]] = []
        for key, item in value.items():
            result.extend(_strings(item, f"{path}.{key}" if path else str(key)))
        return result
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            result.extend(_strings(item, f"{path}[{index}]"))
        return result
    return []


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
    if annotations.get("readOnlyHint") is True and re.search(r"(?:delete|remove|write|send|post|execute)", lower_name):
        report.add("contradictory_annotation", "permission_scope", 7, "warning",
                   f"tools.{name}.annotations.readOnlyHint", "Read-only claim conflicts with tool name")
    schema = tool.get("inputSchema") or {}
    if schema.get("additionalProperties") is True and re.search(r"(?:execute|shell|http|request)", lower_name):
        report.add("permissive_schema", "permission_scope", 4, "warning",
                   f"tools.{name}.inputSchema.additionalProperties", "Privileged tool accepts undeclared fields")


def _scan_source(report: Report, source: Path) -> None:
    if not source.is_dir():
        raise ValueError("source must be an existing directory")
    root = source.resolve()
    total = 0
    files = 0
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
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {".py", ".js", ".ts", ".mjs", ".cjs"}:
            continue
        if any(part in {"node_modules", ".git", "venv", ".venv"} for part in path.relative_to(root).parts):
            continue
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
    lock_exists = any((root / name).is_file() for name in
                      ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "poetry.lock"))
    seen = False
    if package.is_file():
        seen = True
        data = json.loads(package.read_text(encoding="utf-8"))
        scripts = data.get("scripts", {})
        for key in ("preinstall", "install", "postinstall", "prepare"):
            if key in scripts:
                report.add("install_script", "dependency_risk", 6, "warning", f"package.json:scripts.{key}",
                           "Package executes a script during installation")
        if any(str(version).startswith(("*", "^", "~", ">", "<", "latest"))
               for section in ("dependencies", "devDependencies")
               for version in data.get(section, {}).values()):
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
    if not server_id or len(server_id) > 256:
        raise ValueError("invalid server ID")
    if len(tools) > MAX_TOOL_COUNT or len(_canonical(tools)) > MAX_DEFINITION_BYTES:
        raise ValueError("tool catalog too large")
    report = Report(server_id=server_id, coverage={"tool_definitions": "checked", "source_code": "not_available",
                                                    "dependencies": "not_available", "runtime": "not_checked"})
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise ValueError("invalid tool definition")
        if tool["name"] in names:
            raise ValueError("duplicate tool name")
        names.add(tool["name"])
        report.fingerprints[tool["name"]] = fingerprint(tool)
        _scan_tool(report, tool)
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


def compare_baseline(report: Report, baseline: dict[str, Any]) -> list[dict[str, str]]:
    if baseline.get("format") != 1 or baseline.get("serverId") != report.server_id:
        raise ValueError("baseline identity mismatch")
    old = baseline.get("fingerprints")
    if not isinstance(old, dict):
        raise ValueError("invalid baseline")
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
    for field_path, value in _strings(arguments or {}, "arguments"):
        if SENSITIVE_VALUE.search(value):
            findings.append(Finding("secret_in_argument", "runtime_behavior", 5, "critical",
                                    f"{tool_name}.{field_path}", "Credential-like value in tool arguments"))
        if PRIVATE_PATH.search(value) and re.search(r"(?:url|query|body|message|recipient)", field_path, re.I):
            findings.append(Finding("private_path_outbound", "runtime_behavior", 5, "critical",
                                    f"{tool_name}.{field_path}", "Sensitive path in an outbound-looking argument"))
    if response_text:
        if ROLE_OVERRIDE.search(response_text) or (PRIVATE_PATH.search(response_text) and EXFIL_VERB.search(response_text)):
            findings.append(Finding("poisoned_tool_result", "runtime_behavior", 5, "critical",
                                    f"{tool_name}.result", "Tool result contains an instruction attack"))
        if SENSITIVE_VALUE.search(response_text):
            findings.append(Finding("secret_in_result", "runtime_behavior", 5, "critical",
                                    f"{tool_name}.result", "Credential-like value in tool result"))
    return findings


# Only scanner-generated, redacted audit codes are accepted for score updates.
RUNTIME_REASON_CODES = {
    "sensitive_argument": "Credential-like value or sensitive path appeared in tool arguments",
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
        codes = event.get("signals", [])
        if not isinstance(codes, list):
            raise ValueError("invalid runtime signals")
        for code in codes:
            if code not in RUNTIME_REASON_CODES:
                raise ValueError("unknown runtime reason code")
            tool = re.sub(r"[^A-Za-z0-9_.-]", "_", str(event.get("tool", "?"))[:128])
            report.add(code, "runtime_behavior", 5,
                       "critical" if code in {"sensitive_argument", "poisoned_result"} else "warning",
                       f"runtimeAudit[{index}].{tool}", RUNTIME_REASON_CODES[code])
    report.coverage["runtime"] = "checked_mcp_messages"
