"""Evidence-based MCP risk assessment; no server installation or execution."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .source_analysis import analyze_source


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
MAX_INSPECTION_RECORDS = 10_000
MAX_BASE64_TOKEN_CHARS = 4_096
MAX_SUMMARY_PROPERTIES = 200
MAX_TOOL_DEFINITION_NODES = 20_000
MAX_RETAINED_FINDINGS = 1_000
INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
PRIVATE_PATH = re.compile(r"(?:~/\.ssh/|[/\\]\.ssh[/\\]|\.env\b|id_rsa\b|credentials\b|secrets?\b)", re.I)
EXFIL_VERB = re.compile(r"\b(?:send|upload|post|exfiltrate|include|transmit|전송|업로드|포함)\b", re.I)
ROLE_OVERRIDE = re.compile(r"(?:ignore (?:all |the )?(?:previous|prior) instructions|you are now (?:the )?system|<\s*/?system\s*>|developer message:|system prompt:|이전 지시(?:를|사항을)? 무시)", re.I)
EXTERNAL_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
SENSITIVE_VALUE = re.compile(r"(?:-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b)")
HIDDEN_COMMENT = re.compile(r"(?:<!--|\[//\]:\s*#\s*\(|\[comment\]:\s*#\s*\()", re.I)
SYSTEM_TOKEN = re.compile(r"(?:<\|(?:system|developer|assistant|user)\|>|\[INST\]|<<SYS>>)", re.I)
WHITESPACE_PADDING = re.compile(r"(?:[ \t]{128,}|(?:\r?\n){32,})")
BASE64_TOKEN = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{48,}={0,2}(?![A-Za-z0-9+/=])")
DATA_URI = re.compile(r"data:[a-z0-9.+-]+/[a-z0-9.+-]+(?:;[a-z0-9.+-]+=[^;,\s]+)*;base64,", re.I)
SHELL_DEFAULT = re.compile(r"(?:\b(?:bash|sh|cmd|powershell|pwsh)\b|(?:&&|\|\||;\s*)(?:curl|wget|rm|del|invoke-))", re.I)

_CONFUSABLES = {
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0445": "x", "\u0443": "y",
    "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041a": "K", "\u041c": "M", "\u041d": "H", "\u041e": "O", "\u0420": "P", "\u0421": "C", "\u0422": "T", "\u0425": "X",
    "\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0397": "H", "\u0399": "I", "\u039a": "K", "\u039c": "M", "\u039d": "N", "\u039f": "O", "\u03a1": "P", "\u03a4": "T", "\u03a7": "X",
}


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
    inspection_ledger: list[dict[str, Any]] = field(default_factory=list)
    tool_summaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    declared_scopes: list[str] = field(default_factory=list)
    _omitted_penalties: dict[str, int] = field(default_factory=dict, repr=False)
    _omitted_critical: bool = field(default=False, repr=False)
    _omitted_findings: int = field(default=0, repr=False)
    _finding_limit_recorded: bool = field(default=False, repr=False)

    def add(self, code: str, category: str, points: int, severity: str, evidence: str,
            message: str, confidence: str = "observed") -> None:
        if category not in CAPS or points < 0:
            raise ValueError("invalid finding")
        finding = Finding(code, category, points, severity, evidence, message, confidence)
        if finding in self.findings:
            return
        if len(self.findings) >= MAX_RETAINED_FINDINGS:
            self._omitted_penalties[category] = self._omitted_penalties.get(category, 0) + points
            self._omitted_critical = self._omitted_critical or severity == "critical"
            self._omitted_findings += 1
            self.coverage["findings"] = "partial_limit_reached"
            if not self._finding_limit_recorded:
                self.record("finding_output", ".", "partial", "retained_finding_limit_reached",
                            len(self.findings), MAX_RETAINED_FINDINGS)
                self._finding_limit_recorded = True
            return
        self.findings.append(finding)

    def record(self, analyzer: str, path: str, outcome: str, reason: str | None = None,
               observed: int | None = None, limit: int | None = None) -> None:
        if outcome not in {"completed", "partial", "skipped", "failed", "out_of_scope"}:
            raise ValueError("invalid inspection outcome")
        if len(self.inspection_ledger) >= MAX_INSPECTION_RECORDS:
            self.coverage["inspection_ledger"] = "partial_limit_reached"
            return
        item: dict[str, Any] = {"analyzer": analyzer[:128], "path": path[:1_024], "outcome": outcome}
        if reason is not None:
            item["reason"] = reason[:256]
        if observed is not None:
            item["observed"] = observed
        if limit is not None:
            item["limit"] = limit
        self.inspection_ledger.append(item)

    @property
    def analysis_completeness(self) -> dict[str, Any]:
        counts = {status: 0 for status in ("completed", "partial", "skipped", "failed", "out_of_scope")}
        for item in self.inspection_ledger:
            counts[item["outcome"]] += 1
        if self.coverage.get("inspection_ledger") == "partial_limit_reached":
            counts["partial"] += 1
        return {
            "complete": counts["partial"] == counts["skipped"] == counts["failed"] == 0,
            "counts": counts,
            "records": list(self.inspection_ledger),
        }

    @property
    def penalties(self) -> dict[str, int]:
        return {category: min(cap, sum(f.points for f in self.findings if f.category == category)
                                   + self._omitted_penalties.get(category, 0))
                for category, cap in CAPS.items()}

    @property
    def risk_score(self) -> int:
        return sum(self.penalties.values())

    @property
    def security_score(self) -> int:
        return 100 - self.risk_score

    @property
    def verdict(self) -> str:
        if self._omitted_critical or any(f.severity == "critical" for f in self.findings):
            return "critical"
        if self.coverage.get("tool_definitions") != "checked":
            return "insufficient_evidence"
        if self.risk_score >= 40:
            return "high_risk"
        if self.risk_score >= 20 or self.llm_suggestions:
            return "review"
        if (self.coverage.get("source_code") != "checked"
                or self.coverage.get("dependencies") != "checked_manifest_only"
                or self.coverage.get("runtime") != "checked_mcp_messages"
                or self.coverage.get("artifacts") != "checked_no_opaque_artifacts"):
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
            "findingSummary": {"retained": len(self.findings), "omitted": self._omitted_findings},
            "fingerprints": self.fingerprints,
            "llmSuggestions": self.llm_suggestions,
            "analysisCompleteness": self.analysis_completeness,
        }


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def fingerprint(tool: dict[str, Any]) -> str:
    """Bind every exposed tool field, including annotations and metadata."""
    return hashlib.sha256(_canonical(tool)).hexdigest()


def _strings(value: Any, path: str = "", *, max_nodes: int | None = None,
             limit_reached: list[bool] | None = None) -> Iterator[tuple[str, str]]:
    """Yield untrusted keys and values iteratively so hostile depth cannot recurse."""
    def dict_children(base: str, mapping: dict[Any, Any]) -> Iterator[tuple[str, Any]]:
        for key, item in mapping.items():
            key_text = str(key)
            item_path = f"{base}.{key_text}" if base else key_text
            # JSON Schema property names and metadata keys are model-visible attack
            # surface too. Previously they appeared only in the evidence path and
            # were never inspected as text.
            if isinstance(key, str):
                yield f"{item_path}.__key__", key_text
            yield item_path, item

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


def _unicode_scripts(value: str) -> set[str]:
    scripts: set[str] = set()
    for character in value:
        if not character.isalpha():
            continue
        name = unicodedata.name(character, "")
        for script in ("LATIN", "CYRILLIC", "GREEK"):
            if script in name:
                scripts.add(script)
                break
    return scripts


def _has_external_url(value: str) -> bool:
    for match in EXTERNAL_URL.finditer(value):
        if re.match(r"https?://(?:localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?(?:/|$)", match.group(0), re.I):
            continue
        return True
    return False


def _tool_capabilities(name: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    capabilities: set[str] = set()
    if re.search(r"(?:execute|shell|terminal|run_command)", normalized):
        capabilities.add("shell")
    if re.search(r"(?:read_file|list_files|search_files|filesystem)", normalized):
        capabilities.add("file_read")
    if re.search(r"(?:write_file|delete|remove|filesystem)", normalized):
        capabilities.add("file_write")
    if re.search(r"(?:fetch_url|http_request|send_email|post_data|webhook)", normalized):
        capabilities.add("network")
    if re.search(r"(?:get_secret|read_secret|credential|token)", normalized):
        capabilities.add("secret")
    return capabilities


def _scan_tool(report: Report, tool: dict[str, Any]) -> bool:
    name = tool["name"]
    encoded_tokens = 0
    encoded_limit_reported = False
    node_limit_reached = [False]
    for field_path, value in _strings(tool, max_nodes=MAX_TOOL_DEFINITION_NODES,
                                      limit_reached=node_limit_reached):
        # A payload may appear in property descriptions/defaults/_meta, not only description.
        evidence = f"tools.{name}.{field_path}"[:1_024]
        if INVISIBLE.search(value):
            report.add("hidden_unicode", "tool_poisoning", 10, "warning", evidence,
                       "Invisible formatting character in tool definition")
        if PRIVATE_PATH.search(value) and EXFIL_VERB.search(value):
            report.add("credential_exfil_instruction", "tool_poisoning", 20, "critical", evidence,
                       "Instruction combines a sensitive path with data transfer")
        elif ROLE_OVERRIDE.search(value):
            report.add("role_override", "prompt_injection", 10, "critical", evidence,
                       "Tool definition contains an instruction/role override")
        if HIDDEN_COMMENT.search(value):
            report.add("hidden_comment", "tool_poisoning", 8, "warning", evidence,
                       "Tool definition contains an HTML/Markdown hidden-comment marker")
        if SYSTEM_TOKEN.search(value):
            report.add("model_control_token", "prompt_injection", 10, "critical", evidence,
                       "Tool definition contains a model control token")
        if WHITESPACE_PADDING.search(value):
            report.add("whitespace_padding", "tool_poisoning", 5, "warning", evidence,
                       "Tool definition contains unusually long whitespace padding")
        if DATA_URI.search(value):
            report.add("embedded_data_uri", "tool_poisoning", 7, "warning", evidence,
                       "Tool definition contains an embedded base64 data URI")
        for match in BASE64_TOKEN.finditer(value):
            encoded_tokens += 1
            if encoded_tokens > 20:
                if not encoded_limit_reported:
                    report.add("encoded_payload_limit", "tool_poisoning", 4, "warning", f"tools.{name}",
                               "Encoded payload count exceeded the bounded decoder budget")
                    encoded_limit_reported = True
                break
            if match.end() - match.start() > MAX_BASE64_TOKEN_CHARS:
                report.add("encoded_payload_limit", "tool_poisoning", 4, "warning", evidence,
                           "Encoded payload exceeded the bounded decoder size")
                continue
            token = match.group(0)
            try:
                decoded = base64.b64decode(token, validate=True).decode("utf-8", errors="strict")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
            if not decoded or sum(character.isprintable() or character.isspace() for character in decoded) / len(decoded) < 0.85:
                continue
            report.add("encoded_text_payload", "tool_poisoning", 7, "warning", evidence,
                       "Tool definition contains a valid UTF-8 base64 text payload")
            if ROLE_OVERRIDE.search(decoded) or (PRIVATE_PATH.search(decoded) and EXFIL_VERB.search(decoded)):
                report.add("encoded_instruction", "prompt_injection", 10, "critical", evidence,
                           "Decoded tool-definition text contains an instruction attack")
        if field_path.endswith(".description") and len(value) > 2_048:
            report.add("excessive_parameter_description", "tool_poisoning", 4, "warning", evidence,
                       "Parameter description exceeds the bounded review threshold")
        if (re.search(r"(?:^|\.)default(?:\.|\[|$)", field_path)
                and (_has_external_url(value) or SHELL_DEFAULT.search(value))):
            report.add("suspicious_parameter_default", "permission_scope", 5, "warning", evidence,
                       "Parameter default contains an external URL or shell-like command")
        if field_path == "name" or field_path.endswith(".__key__"):
            if any(character in _CONFUSABLES for character in value):
                report.add("unicode_confusable", "tool_poisoning", 8, "warning", evidence,
                           "Identifier contains a known Unicode lookalike character")
            if len(_unicode_scripts(value)) > 1:
                report.add("mixed_script_identifier", "tool_poisoning", 6, "warning", evidence,
                           "Identifier mixes Latin, Cyrillic, or Greek scripts")

    lower_name = name.lower()
    normalized_name = re.sub(r"[^a-z0-9]+", "_", lower_name).strip("_")
    if re.search(r"(?:execute|shell|terminal|run_command|delete|remove|write_file)", normalized_name):
        report.add("privileged_tool", "permission_scope", 5, "warning", f"tools.{name}.name",
                   "Tool name suggests a privileged action", "inferred")
    if re.search(r"(?:read_file|write_file|list_files|search_files|filesystem)", normalized_name):
        report.add("filesystem_tool", "file_system_access", 4, "info", f"tools.{name}.name",
                   "Tool appears to have filesystem access", "inferred")
    if re.search(r"(?:fetch_url|http_request|send_email|post_data|webhook)", normalized_name):
        report.add("network_tool", "network_access", 3, "info", f"tools.{name}.name",
                   "Tool appears to reach the network", "inferred")
    if re.search(r"(?:get_secret|read_secret|credential|token)", normalized_name):
        report.add("secret_tool", "secret_access", 5, "warning", f"tools.{name}.name",
                   "Tool appears to access credentials", "inferred")
    annotations = tool.get("annotations") or {}
    if not isinstance(annotations, dict):
        raise ValueError("tool annotations must be an object")
    if annotations.get("readOnlyHint") is True and re.search(
            r"(?:delete|remove|write|send|post|execute)", normalized_name):
        report.add("contradictory_annotation", "permission_scope", 7, "warning",
                   f"tools.{name}.annotations.readOnlyHint", "Read-only claim conflicts with tool name")
    schema = tool.get("inputSchema") or {}
    if not isinstance(schema, dict):
        raise ValueError("tool inputSchema must be an object")
    # JSON Schema defaults additionalProperties to true when omitted; a schema
    # object also permits additional fields under that schema. Only explicit false
    # closes the input shape.
    if schema.get("additionalProperties", True) is not False and re.search(
            r"(?:execute|shell|http|request)", normalized_name):
        report.add("permissive_schema", "permission_scope", 4, "warning",
                   f"tools.{name}.inputSchema.additionalProperties", "Privileged tool accepts undeclared fields")
    return not node_limit_reached[0]


def _tool_summary(report: Report, tool: dict[str, Any]) -> dict[str, Any]:
    name = tool["name"]
    schema = tool.get("inputSchema") or {}
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    property_summary: dict[str, dict[str, Any]] = {}
    capabilities = _tool_capabilities(name)
    if isinstance(properties, dict):
        eligible_property_names = sorted(
            name for name in properties if isinstance(name, str) and len(name) <= 1_024
        )
        for property_name in eligible_property_names[:MAX_SUMMARY_PROPERTIES]:
            definition = properties[property_name]
            if not isinstance(definition, dict):
                definition = {}
            default = definition.get("default")
            property_summary[property_name] = {
                "type": definition.get("type") if isinstance(definition.get("type"), str) else None,
                "hasDefault": "default" in definition,
                "suspiciousDefault": isinstance(default, str) and (
                    _has_external_url(default) or SHELL_DEFAULT.search(default) is not None
                ),
            }
            if isinstance(default, str) and _has_external_url(default):
                capabilities.add("network")
            if isinstance(default, str) and SHELL_DEFAULT.search(default):
                capabilities.add("shell")
    for property_name, definition in property_summary.items():
        normalized_property = re.sub(r"[^a-z0-9]+", "_", property_name.lower()).strip("_")
        if re.search(r"(?:command|shell|script|executable)", normalized_property):
            capabilities.add("shell")
        if re.search(r"(?:url|uri|endpoint|webhook|recipient|email)", normalized_property):
            capabilities.add("network")
        if re.search(r"(?:secret|credential|password|token|api_key)", normalized_property):
            capabilities.add("secret")
        if re.search(r"(?:output|destination|target).*(?:path|file)|(?:write|save).*(?:path|file)", normalized_property):
            capabilities.add("file_write")
        elif re.search(r"(?:path|file|directory|folder)", normalized_property):
            capabilities.add("file_read")
    prefix = f"tools.{name}."
    poisoning = sorted({
        finding.code for finding in report.findings
        if finding.evidence.startswith(prefix) and finding.category in {"tool_poisoning", "prompt_injection"}
    })
    annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    return {
        "capabilities": sorted(capabilities),
        "readOnlyHint": annotations.get("readOnlyHint"),
        "destructiveHint": annotations.get("destructiveHint"),
        "additionalPropertiesOpen": schema.get("additionalProperties", True) is not False if isinstance(schema, dict) else True,
        "required": sorted(item for item in required if isinstance(item, str) and len(item) <= 1_024)[:MAX_SUMMARY_PROPERTIES]
        if isinstance(required, list) else [],
        "properties": property_summary,
        "propertySummaryTruncated": (
            isinstance(properties, dict)
            and (len(properties) > MAX_SUMMARY_PROPERTIES or len(property_summary) != len(properties))
        ),
        "poisoningSignals": poisoning,
    }


def _scan_source(report: Report, source: Path) -> set[str]:
    if not source.is_dir():
        raise ValueError("source must be an existing directory")
    result = analyze_source(
        source,
        max_files=MAX_SOURCE_FILES,
        max_bytes=MAX_SOURCE_BYTES,
        max_entries=MAX_SOURCE_ENTRIES,
    )
    for signal in result.signals:
        report.add(signal.code, signal.category, signal.points, signal.severity, signal.evidence,
                   signal.message, signal.confidence)
    for category, points in sorted(result.omitted_signal_points.items()):
        count = result.omitted_signal_counts[category]
        report.add(
            "source_signal_output_truncated",
            category,
            points,
            "critical" if category in result.omitted_critical_categories else "warning",
            "sourceAnalysis",
            f"{count} additional source signals were scored but omitted from detailed output",
            "deterministic",
        )
    for record in result.records:
        report.record(record.analyzer, record.path, record.outcome, record.reason,
                      record.observed, record.limit)
    report.coverage["source_scope"] = result.source_scope
    report.coverage["source_code"] = "checked" if result.source_state == "checked" else "partial_limit_or_error"
    report.coverage["artifacts"] = result.artifact_state
    return result.capabilities


def _scan_dependencies(report: Report, source: Path) -> None:
    root = source.resolve()
    package = root / "package.json"
    requirements = root / "requirements.txt"
    if any(path.is_symlink() for path in (package, requirements)):
        report.coverage["dependencies"] = "partial_symlink_skipped"
        for path in (package, requirements):
            if path.is_symlink():
                report.record("dependency_manifest", path.name, "skipped", "symlink_not_followed")
        return
    lock_exists = any(path.is_file() and not path.is_symlink() for path in
                      (root / name for name in
                       ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml",
                        "yarn.lock", "uv.lock", "poetry.lock")))
    seen = False
    complete = True
    try:
        manifest_bytes = sum(path.stat().st_size for path in (package, requirements) if path.is_file())
    except OSError as exc:
        report.coverage["dependencies"] = "partial_manifest_error"
        report.record("dependency_manifest", ".", "failed", f"stat:{type(exc).__name__}")
        return
    if manifest_bytes > MAX_MANIFEST_BYTES:
        report.coverage["dependencies"] = "partial_limit_reached"
        report.record("dependency_manifest", ".", "partial", "manifest_byte_limit_reached",
                      manifest_bytes, MAX_MANIFEST_BYTES)
        return
    if package.is_file():
        seen = True
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("root_not_object")
            scripts = data.get("scripts", {})
            if not isinstance(scripts, dict):
                raise ValueError("scripts_not_object")
            dependency_sections = []
            for section in ("dependencies", "devDependencies"):
                values = data.get(section, {})
                if not isinstance(values, dict):
                    raise ValueError(f"{section}_not_object")
                dependency_sections.append(values)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            complete = False
            report.record("dependency_manifest", "package.json", "failed",
                          f"parse:{str(exc)[:120] or type(exc).__name__}")
        else:
            for key in ("preinstall", "install", "postinstall", "prepare"):
                if key in scripts:
                    report.add("install_script", "dependency_risk", 6, "warning", f"package.json:scripts.{key}",
                               "Package executes a script during installation")
            if any(str(version).startswith(("*", "^", "~", ">", "<", "latest"))
                   for values in dependency_sections for version in values.values()):
                report.add("floating_dependency", "dependency_risk", 3, "info", "package.json:dependencies",
                           "Dependency versions are not exact", "inferred")
            report.record("dependency_manifest", "package.json", "completed")
    else:
        report.record("dependency_manifest", "package.json", "out_of_scope", "not_present")
    if requirements.is_file():
        seen = True
        try:
            requirement_lines = requirements.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            complete = False
            report.record("dependency_manifest", "requirements.txt", "failed",
                          f"read:{type(exc).__name__}")
        else:
            for line_no, line in enumerate(requirement_lines, 1):
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "==" not in stripped and " @ " not in stripped:
                    report.add("floating_dependency", "dependency_risk", 3, "info",
                               f"requirements.txt:{line_no}", "Dependency is not pinned", "inferred")
            report.record("dependency_manifest", "requirements.txt", "completed")
    else:
        report.record("dependency_manifest", "requirements.txt", "out_of_scope", "not_present")
    if seen and not lock_exists:
        report.add("missing_lockfile", "dependency_risk", 3, "info", str(root),
                   "No supported dependency lockfile found", "inferred")
    report.record("dependency_lock", ".", "completed" if seen else "out_of_scope",
                  "present" if lock_exists else "not_present")
    report.coverage["dependencies"] = (
        "checked_manifest_only" if seen and complete else
        "partial_manifest_error" if seen else
        "not_available"
    )


def _scope_capabilities(scopes: list[str]) -> tuple[set[str], bool]:
    capabilities: set[str] = set()
    wildcard = False
    for scope in scopes:
        normalized = scope.lower().strip()
        tokens = {token for token in re.split(r"[^a-z0-9]+", normalized) if token}
        if normalized in {"*", "all", "full", "admin"} or normalized.endswith(":*") or normalized.endswith("/*"):
            wildcard = True
        if tokens.intersection({"shell", "exec", "execute", "terminal", "command"}):
            capabilities.add("shell")
        if tokens.intersection({"network", "internet", "http", "https", "fetch", "webhook"}):
            capabilities.add("network")
        if tokens.intersection({"secret", "secrets", "credential", "credentials", "token", "tokens"}):
            capabilities.add("secret")
        if tokens.intersection({"env", "environment"}):
            capabilities.add("environment")
        file_context = bool(tokens.intersection({"file", "files", "filesystem", "fs", "repo", "repository", "workspace"}))
        if file_context and tokens.intersection({"read", "list", "search", "repo", "repository", "workspace", "filesystem"}):
            capabilities.add("file_read")
        if file_context and tokens.intersection({"write", "edit", "delete", "remove", "repo", "repository", "workspace", "filesystem"}):
            capabilities.add("file_write")
    return capabilities, wildcard


def _reconcile_scopes(report: Report, inferred: set[str], scopes: list[str]) -> None:
    declared, wildcard = _scope_capabilities(scopes)
    if wildcard:
        report.add("wildcard_scope", "permission_scope", 10, "warning", "authorization.scopes",
                   "Authorization uses a wildcard or unrestricted scope", "observed")
    if inferred and not scopes:
        report.add("missing_scope_declaration", "permission_scope", 6, "warning", "authorization.scopes",
                   "Source capabilities were inferred but no authorization scopes were declared", "inferred")
        return
    if not wildcard:
        for capability in sorted(inferred - declared):
            report.add("underdeclared_capability", "permission_scope", 6, "warning",
                       f"authorization.scopes:{capability}",
                       f"Source appears to use {capability}, but the declared scopes do not describe it", "inferred")
    source_complete = (report.coverage.get("source_code") == "checked"
                       and report.coverage.get("artifacts") == "checked_no_opaque_artifacts")
    if source_complete and not wildcard:
        for capability in sorted(declared - inferred):
            report.add("overdeclared_capability", "permission_scope", 2, "info",
                       f"authorization.scopes:{capability}",
                       f"Declared {capability} capability was not observed in the supplied source", "inferred")


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
                                                    "dependencies": "not_available", "runtime": "not_checked",
                                                    "artifacts": "not_available"})
    names: set[str] = set()
    for tool in tools:
        if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                or not tool["name"] or len(tool["name"]) > 256):
            raise ValueError("invalid tool definition")
        if tool["name"] in names:
            raise ValueError("duplicate tool name")
        names.add(tool["name"])
        report.fingerprints[tool["name"]] = fingerprint(tool)
        tool_scan_complete = _scan_tool(report, tool)
        report.tool_summaries[tool["name"]] = _tool_summary(report, tool)
        if tool_scan_complete:
            report.record("tool_definition", tool["name"], "completed")
        else:
            report.coverage["tool_definitions"] = "partial_limit_reached"
            report.record("tool_definition", tool["name"], "partial", "node_limit_reached",
                          MAX_TOOL_DEFINITION_NODES, MAX_TOOL_DEFINITION_NODES)
    if scopes is not None and (not isinstance(scopes, list) or len(scopes) > 100
                               or any(not isinstance(scope, str) or len(scope) > 256
                                      for scope in scopes)):
        raise ValueError("invalid authorization scopes")
    report.declared_scopes = list(scopes or [])
    for scope in report.declared_scopes:
        if re.search(r"(?:admin|full|write|delete|repo$|all)", scope, re.I):
            report.add("broad_scope", "permission_scope", 8, "warning", "authorization.scopes",
                       "Granted scope may permit modification", "inferred")
            break
    if source is not None:
        inferred_capabilities = _scan_source(report, source)
        _scan_dependencies(report, source)
        _reconcile_scopes(report, inferred_capabilities, report.declared_scopes)
    else:
        report.record("source_inventory", ".", "out_of_scope", "source_not_supplied")
        report.record("dependency_manifest", ".", "out_of_scope", "source_not_supplied")
    return report


def _baseline_payload(report: Report, format_version: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "format": format_version,
        "serverId": report.server_id,
        "fingerprints": dict(report.fingerprints),
        "scopes": list(report.declared_scopes),
        "toolSummaries": {},
    }
    summaries: dict[str, Any] = payload["toolSummaries"]
    included = 0
    for name in sorted(report.tool_summaries):
        summaries[name] = report.tool_summaries[name]
        payload["summaryCoverage"] = {
            "included": included + 1,
            "total": len(report.tool_summaries),
            "complete": included + 1 == len(report.tool_summaries),
        }
        serialized_size = len((json.dumps(payload, ensure_ascii=True, indent=2) + "\n").encode("utf-8"))
        if serialized_size > MAX_BASELINE_BYTES - 1_024:
            summaries.pop(name)
            break
        included += 1
    payload["summaryCoverage"] = {
        "included": included,
        "total": len(report.tool_summaries),
        "complete": included == len(report.tool_summaries),
    }
    return payload


def make_baseline(report: Report) -> dict[str, Any]:
    """A reference snapshot for drift detection, not an approval or safety claim."""
    return _baseline_payload(report, 1)


def _validate_integrity_key(integrity_key: bytes) -> None:
    if len(integrity_key) < 32:
        raise ValueError("integrity key must be at least 32 bytes")


def make_signed_baseline(report: Report, integrity_key: bytes) -> dict[str, Any]:
    """Create an HMAC-authenticated reference; keep its key outside the baseline."""
    _validate_integrity_key(integrity_key)
    baseline = _baseline_payload(report, 2)
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
            raise ValueError("integrity key file is too large")
        key = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError("integrity key file does not exist") from exc
    _validate_integrity_key(key)
    return key


def _valid_tool_summary(summary: dict[str, Any]) -> bool:
    capabilities = summary.get("capabilities", [])
    required = summary.get("required", [])
    poisoning = summary.get("poisoningSignals", [])
    properties = summary.get("properties", {})
    if (not isinstance(capabilities, list) or len(capabilities) > 10
            or any(not isinstance(item, str) or len(item) > 64 for item in capabilities)):
        return False
    if (not isinstance(required, list) or len(required) > MAX_SUMMARY_PROPERTIES
            or any(not isinstance(item, str) or len(item) > 1_024 for item in required)):
        return False
    if (not isinstance(poisoning, list) or len(poisoning) > 100
            or any(not isinstance(item, str) or len(item) > 128 for item in poisoning)):
        return False
    if not isinstance(properties, dict) or len(properties) > MAX_SUMMARY_PROPERTIES:
        return False
    for name, definition in properties.items():
        if not isinstance(name, str) or len(name) > 1_024 or not isinstance(definition, dict):
            return False
        if set(definition) - {"type", "hasDefault", "suspiciousDefault"}:
            return False
        if definition.get("type") is not None and not isinstance(definition.get("type"), str):
            return False
        if not isinstance(definition.get("hasDefault"), bool):
            return False
        if not isinstance(definition.get("suspiciousDefault"), bool):
            return False
    for key in ("readOnlyHint", "destructiveHint"):
        if summary.get(key) is not None and not isinstance(summary.get(key), bool):
            return False
    for key in ("additionalPropertiesOpen", "propertySummaryTruncated"):
        if not isinstance(summary.get(key), bool):
            return False
    return True


def verify_baseline(baseline: dict[str, Any], *, server_id: str | None = None,
                    integrity_key: bytes | None = None,
                    require_integrity: bool = False) -> dict[str, str]:
    """Validate baseline structure, identity, fingerprints, and optional HMAC."""
    if not isinstance(baseline, dict) or baseline.get("format") not in {1, 2}:
        raise ValueError("invalid baseline format")
    try:
        if len(_canonical(baseline)) > MAX_BASELINE_BYTES:
            raise ValueError("baseline is too large")
    except (TypeError, RecursionError, UnicodeError) as exc:
        raise ValueError("invalid baseline data") from exc
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
    scopes = baseline.get("scopes", [])
    if (not isinstance(scopes, list) or len(scopes) > 100
            or any(not isinstance(scope, str) or len(scope) > 256 for scope in scopes)):
        raise ValueError("invalid baseline scopes")
    summaries = baseline.get("toolSummaries", {})
    if (not isinstance(summaries, dict) or len(summaries) > MAX_TOOL_COUNT
            or any(not isinstance(name, str) or name not in fingerprints or not isinstance(summary, dict)
                   or not _valid_tool_summary(summary)
                   for name, summary in summaries.items())):
        raise ValueError("invalid baseline tool summaries")
    summary_coverage = baseline.get("summaryCoverage")
    if summary_coverage is not None and (
            not isinstance(summary_coverage, dict)
            or set(summary_coverage) != {"included", "total", "complete"}
            or not isinstance(summary_coverage.get("included"), int)
            or isinstance(summary_coverage.get("included"), bool)
            or not isinstance(summary_coverage.get("total"), int)
            or isinstance(summary_coverage.get("total"), bool)
            or not isinstance(summary_coverage.get("complete"), bool)
            or summary_coverage["included"] != len(summaries)
            or summary_coverage["total"] != len(fingerprints)
            or summary_coverage["complete"] != (len(summaries) == len(fingerprints))):
        raise ValueError("invalid baseline summary coverage")
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


def _semantic_tool_changes(before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, str | None]]:
    changes: list[tuple[str, str | None]] = []
    before_capabilities = set(before.get("capabilities", []))
    after_capabilities = set(after.get("capabilities", []))
    for capability in sorted(after_capabilities - before_capabilities):
        changes.append(("permission_expanded", capability))
    if before.get("readOnlyHint") is True and after.get("readOnlyHint") is not True:
        changes.append(("read_only_hint_weakened", None))
    if before.get("additionalPropertiesOpen") is False and after.get("additionalPropertiesOpen") is True:
        changes.append(("input_schema_opened", None))
    before_properties = before.get("properties", {})
    after_properties = after.get("properties", {})
    if isinstance(before_properties, dict) and isinstance(after_properties, dict):
        for name in sorted(set(after_properties) - set(before_properties)):
            changes.append(("parameter_added", name))
            definition = after_properties[name]
            if isinstance(definition, dict) and definition.get("suspiciousDefault"):
                changes.append(("suspicious_default_added", name))
        for name in sorted(set(before_properties) & set(after_properties)):
            old_definition = before_properties[name]
            new_definition = after_properties[name]
            if (isinstance(old_definition, dict) and isinstance(new_definition, dict)
                    and not old_definition.get("suspiciousDefault") and new_definition.get("suspiciousDefault")):
                changes.append(("suspicious_default_added", name))
    for name in sorted(set(after.get("required", [])) - set(before.get("required", []))):
        changes.append(("required_parameter_added", name))
    for signal in sorted(set(after.get("poisoningSignals", [])) - set(before.get("poisoningSignals", []))):
        changes.append(("instruction_risk_added", signal))
    if not before.get("destructiveHint") and after.get("destructiveHint") is True:
        changes.append(("destructive_hint_enabled", None))
    return changes


def compare_baseline(report: Report, baseline: dict[str, Any], *,
                     integrity_key: bytes | None = None,
                     require_integrity: bool = False) -> list[dict[str, Any]]:
    old = verify_baseline(baseline, server_id=report.server_id, integrity_key=integrity_key,
                          require_integrity=require_integrity)
    changes: list[dict[str, Any]] = []
    old_summaries = baseline.get("toolSummaries", {})
    for name in sorted(set(old) | set(report.fingerprints)):
        before = old.get(name)
        after = report.fingerprints.get(name)
        if before != after:
            if before is None:
                changes.append({"tool": name, "change": "added"})
            elif after is None:
                changes.append({"tool": name, "change": "removed"})
            elif name in old_summaries and name in report.tool_summaries:
                semantic = _semantic_tool_changes(old_summaries[name], report.tool_summaries[name])
                if semantic:
                    for change, detail in semantic:
                        item = {"tool": name, "change": change}
                        if detail is not None:
                            item["detail"] = detail
                        changes.append(item)
                else:
                    changes.append({"tool": name, "change": "modified"})
            else:
                changes.append({"tool": name, "change": "modified"})
    if "scopes" in baseline:
        old_scope_capabilities, old_wildcard = _scope_capabilities(baseline.get("scopes", []))
        new_scope_capabilities, new_wildcard = _scope_capabilities(report.declared_scopes)
        if new_wildcard and not old_wildcard:
            changes.append({"tool": "$authorization", "change": "wildcard_scope_added"})
        for capability in sorted(new_scope_capabilities - old_scope_capabilities):
            changes.append({"tool": "$authorization", "change": "scope_expanded", "detail": capability})
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


def sign_runtime_event(event: dict[str, Any], integrity_key: bytes) -> dict[str, Any]:
    """Return an HMAC-authenticated copy of one redacted proxy event."""
    _validate_integrity_key(integrity_key)
    if not isinstance(event, dict):
        raise ValueError("invalid runtime event")
    payload = {key: value for key, value in event.items() if key != "integrity"}
    try:
        digest = hmac.new(integrity_key, _canonical(payload), hashlib.sha256).hexdigest()
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ValueError("runtime event is not valid JSON data") from exc
    return {**payload, "integrity": {"algorithm": "hmac-sha256", "digest": digest}}


def _verify_runtime_event(event: dict[str, Any], integrity_key: bytes) -> None:
    _validate_integrity_key(integrity_key)
    integrity = event.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "hmac-sha256":
        raise ValueError("runtime audit integrity metadata is missing")
    supplied = integrity.get("digest")
    payload = {key: value for key, value in event.items() if key != "integrity"}
    try:
        expected = hmac.new(integrity_key, _canonical(payload), hashlib.sha256).hexdigest()
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ValueError("runtime event is not valid JSON data") from exc
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
        raise ValueError("runtime audit integrity check failed")


def _add_runtime_audit(report: Report, events: list[dict[str, Any]], *,
                       integrity_key: bytes | None = None) -> None:
    if len(events) > 10_000:
        raise ValueError("runtime audit exceeds event limit")
    trusted = integrity_key is not None
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError("invalid runtime event")
        if event.get("decision") not in {"listed", "forwarded"}:
            raise ValueError("invalid runtime decision")
        codes = event.get("signals", [])
        if not isinstance(codes, list) or len(codes) > len(RUNTIME_REASON_CODES):
            raise ValueError("invalid runtime signals")
        if integrity_key is not None:
            _verify_runtime_event(event, integrity_key)
            if event.get("serverId") != report.server_id:
                raise ValueError("runtime audit server ID does not match report")
        for code in codes:
            if not isinstance(code, str) or code not in RUNTIME_REASON_CODES:
                raise ValueError("unknown runtime reason code")
            tool = re.sub(r"[^A-Za-z0-9_.-]", "_", str(event.get("tool", "?"))[:128])
            report.add(code, "runtime_behavior", 5,
                       "critical" if code in {"sensitive_argument", "poisoned_result"} else "warning",
                       f"runtimeAudit[{index}].{tool}", RUNTIME_REASON_CODES[code],
                       "observed" if trusted else "unverified")
    if not trusted:
        report.coverage["runtime"] = "unverified_caller_supplied" if events else "no_messages_observed"
        report.record("runtime_audit", "runtimeAudit", "partial" if events else "skipped",
                      "unsigned_caller_input" if events else "no_messages_observed", len(events), 10_000)
    elif any(event.get("decision") == "forwarded" for event in events):
        report.coverage["runtime"] = "checked_mcp_messages"
        report.record("runtime_audit", "runtimeAudit", "completed", observed=len(events), limit=10_000)
    else:
        report.coverage["runtime"] = "metadata_only_no_calls" if events else "no_messages_observed"
        report.record("runtime_audit", "runtimeAudit", "partial" if events else "skipped",
                      "metadata_only" if events else "no_messages_observed", len(events), 10_000)


def add_runtime_audit(report: Report, events: list[dict[str, Any]], *,
                      integrity_key: bytes | None = None) -> None:
    """Add redacted events; unsigned caller input never establishes checked coverage."""
    _add_runtime_audit(report, events, integrity_key=integrity_key)
