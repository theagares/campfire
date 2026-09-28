from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


MAX_AST_NODES = 50_000
MAX_SOURCE_SIGNALS = 500
MAX_TAINT_NAMES = 1_000
MAX_TAINT_NODE_VISITS = 200_000
MAX_ARTIFACT_PEEK_BYTES = 256_000
ARTIFACT_PEEK_SIZE = 4_096

_SOURCE_SUFFIXES = {".py", ".js", ".ts", ".mjs", ".cjs", ".sh", ".ps1", ".bat", ".cmd"}
_CONFIG_SUFFIXES = {".json", ".jsonc", ".toml", ".yaml", ".yml"}
_ARCHIVE_SUFFIXES = {".zip", ".jar", ".whl", ".tar", ".gz", ".tgz", ".7z", ".rar"}
_BYTECODE_SUFFIXES = {".pyc", ".pyo"}
_BINARY_SUFFIXES = {".exe", ".dll", ".so", ".dylib", ".com"}
_EXCLUDED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__"}

_SOURCE_PATTERNS = (
    (
        re.compile(r"(?:/etc/(?:shadow|passwd)|\.ssh/|\.aws/credentials)", re.I),
        "sensitive_path",
        "secret_access",
        8,
        "info",
        {"file_read", "secret"},
    ),
    (
        re.compile(r"(?:process\.env|os\.(?:environ|getenv)|dotenv|api[_-]?key|secret|token)", re.I),
        "environment_access",
        "secret_access",
        6,
        "warning",
        {"environment", "secret"},
    ),
    (
        re.compile(r"(?:subprocess\.|child_process|os\.(?:system|popen|exec)|Runtime\.getRuntime\(\)\.exec|\bexec\s*\()", re.I),
        "command_execution",
        "permission_scope",
        7,
        "critical",
        {"shell"},
    ),
    (
        re.compile(r"(?:https?://|requests\.|urllib|fetch\s*\(|axios|httpx|net\.|socket\.)", re.I),
        "network_code",
        "network_access",
        5,
        "warning",
        {"network"},
    ),
    (
        re.compile(r"(?:\bopen\s*\(|readFile|writeFile|Path\s*\(|fs\.)", re.I),
        "filesystem_code",
        "file_system_access",
        4,
        "info",
        {"file_read", "file_write"},
    ),
)

_EXEC_CALLS = {
    "exec",
    "eval",
    "compile",
    "__import__",
    "builtins.exec",
    "builtins.eval",
    "importlib.import_module",
    "subprocess.run",
    "subprocess.call",
    "subprocess.Popen",
    "subprocess.check_call",
    "subprocess.check_output",
    "os.system",
    "os.popen",
    "os.execv",
    "os.execve",
    "os.spawnv",
}
_DESERIALIZE_CALLS = {
    "pickle.load",
    "pickle.loads",
    "marshal.load",
    "marshal.loads",
    "yaml.load",
    "dill.load",
    "dill.loads",
}
_NETWORK_CALL_PREFIXES = (
    "requests.",
    "httpx.",
    "urllib.request.",
    "aiohttp.",
    "socket.",
)
_WRITE_CALLS = {
    "pathlib.Path.write_text",
    "pathlib.Path.write_bytes",
    "Path.write_text",
    "Path.write_bytes",
    "shutil.copy",
    "shutil.copyfile",
}


@dataclass(frozen=True)
class SourceSignal:
    code: str
    category: str
    points: int
    severity: str
    evidence: str
    message: str
    confidence: str = "deterministic"


@dataclass(frozen=True)
class InspectionRecord:
    analyzer: str
    path: str
    outcome: str
    reason: str | None = None
    observed: int | None = None
    limit: int | None = None


@dataclass
class SourceAnalysisResult:
    signals: list[SourceSignal] = field(default_factory=list)
    records: list[InspectionRecord] = field(default_factory=list)
    capabilities: set[str] = field(default_factory=set)
    source_state: str = "checked"
    artifact_state: str = "checked_no_opaque_artifacts"
    source_scope: str = ""
    source_files: int = 0
    source_bytes: int = 0
    entries: int = 0
    artifact_peek_bytes: int = 0
    omitted_signal_points: dict[str, int] = field(default_factory=dict)
    omitted_signal_counts: dict[str, int] = field(default_factory=dict)
    omitted_critical_categories: set[str] = field(default_factory=set)
    _signal_limit_recorded: bool = False

    def add_signal(self, signal: SourceSignal) -> None:
        if len(self.signals) < MAX_SOURCE_SIGNALS:
            self.signals.append(signal)
            return
        self.omitted_signal_points[signal.category] = (
            self.omitted_signal_points.get(signal.category, 0) + signal.points
        )
        self.omitted_signal_counts[signal.category] = self.omitted_signal_counts.get(signal.category, 0) + 1
        if signal.severity == "critical":
            self.omitted_critical_categories.add(signal.category)
        self.source_state = "partial"
        if not self._signal_limit_recorded:
            self.records.append(
                InspectionRecord(
                    "source_findings",
                    ".",
                    "partial",
                    "finding_limit_reached",
                    len(self.signals),
                    MAX_SOURCE_SIGNALS,
                )
            )
            self._signal_limit_recorded = True


def _relative(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    return relative[:1_024]


def _call_name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value, aliases)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func, aliases)
    return ""


def _names_in(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _source_taints(node: ast.AST, aliases: dict[str, str], tainted: dict[str, set[str]],
                   visit_budget: list[int]) -> set[str]:
    result: set[str] = set()
    for child in ast.walk(node):
        if visit_budget[0] <= 0:
            break
        visit_budget[0] -= 1
        if isinstance(child, ast.Name):
            result.update(tainted.get(child.id, ()))
        if isinstance(child, ast.Call):
            call = _call_name(child.func, aliases)
            lowered = call.lower()
            if call in {"input", "sys.stdin.read", "sys.stdin.readline"}:
                result.add("user")
            if lowered in {"os.getenv", "os.environ.get"}:
                result.add("credential")
            if call in {"open", "pathlib.Path.read_text", "pathlib.Path.read_bytes", "Path.read_text", "Path.read_bytes"}:
                result.add("file")
            if any(call.startswith(prefix) for prefix in _NETWORK_CALL_PREFIXES):
                result.add("network")
        elif isinstance(child, ast.Subscript) and _call_name(child.value, aliases) in {"os.environ", "environ"}:
            result.add("credential")
    return result


def _python_analysis(text: str, rel: str, result: SourceAnalysisResult) -> None:
    try:
        tree = ast.parse(text, filename=rel)
    except (SyntaxError, ValueError) as exc:
        reason = f"parse_error:{type(exc).__name__}"
        result.records.append(InspectionRecord("python_ast", rel, "skipped", reason))
        result.records.append(InspectionRecord("taint_tracking", rel, "skipped", reason))
        result.source_state = "partial"
        return

    nodes: list[ast.AST] = []
    exceeded = False
    for index, node in enumerate(ast.walk(tree), start=1):
        if index > MAX_AST_NODES:
            exceeded = True
            break
        nodes.append(node)

    aliases: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"

    seen_calls: set[tuple[str, int]] = set()
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        call = _call_name(node.func, aliases)
        line = getattr(node, "lineno", 1)
        marker = (call, line)
        if marker in seen_calls:
            continue
        seen_calls.add(marker)
        if call in _EXEC_CALLS:
            result.capabilities.add("shell")
            result.add_signal(
                SourceSignal(
                    "dynamic_or_command_execution",
                    "permission_scope",
                    7,
                    "critical",
                    f"{rel}:{line}",
                    f"Python AST contains execution primitive {call}.",
                )
            )
        if call in _DESERIALIZE_CALLS:
            result.add_signal(
                SourceSignal(
                    "unsafe_deserialization",
                    "permission_scope",
                    6,
                    "critical",
                    f"{rel}:{line}",
                    f"Python AST contains unsafe deserialization primitive {call}.",
                )
            )

    tainted: dict[str, set[str]] = {}
    taint_visit_budget = [MAX_TAINT_NODE_VISITS]
    taint_name_limit = False
    ordered = sorted(nodes, key=lambda item: (getattr(item, "lineno", 0), getattr(item, "col_offset", 0)))
    for node in ordered:
        if taint_visit_budget[0] <= 0:
            break
        value: ast.AST | None = None
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            value = node.value
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value = node.value
            targets = [node.target]
        if value is None:
            continue
        sources = _source_taints(value, aliases, tainted, taint_visit_budget)
        for target in targets:
            for name in _names_in(target):
                if len(tainted) >= MAX_TAINT_NAMES and name not in tainted:
                    taint_name_limit = True
                    continue
                if sources:
                    tainted[name] = set(sources)
                else:
                    tainted.pop(name, None)

    flow_seen: set[tuple[str, int]] = set()
    for node in nodes:
        if taint_visit_budget[0] <= 0:
            break
        if not isinstance(node, ast.Call):
            continue
        call = _call_name(node.func, aliases)
        line = getattr(node, "lineno", 1)
        supplied = set()
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            supplied.update(_source_taints(argument, aliases, tainted, taint_visit_budget))
        if not supplied:
            continue
        if any(call.startswith(prefix) for prefix in _NETWORK_CALL_PREFIXES) and supplied.intersection({"credential", "file"}):
            marker = ("credential_exfiltration_flow", line)
            if marker not in flow_seen:
                result.add_signal(
                    SourceSignal(
                        marker[0],
                        "secret_access",
                        12,
                        "critical",
                        f"{rel}:{line}",
                        "A bounded static flow links credential or file-derived data to a network call.",
                        "inferred",
                    )
                )
                flow_seen.add(marker)
        if call in _EXEC_CALLS and supplied.intersection({"user", "network"}):
            marker = ("untrusted_code_execution_flow", line)
            if marker not in flow_seen:
                result.add_signal(
                    SourceSignal(
                        marker[0],
                        "permission_scope",
                        10,
                        "critical",
                        f"{rel}:{line}",
                        "A bounded static flow links user or network-derived data to code or command execution.",
                        "inferred",
                    )
                )
                flow_seen.add(marker)
        if call in _WRITE_CALLS and supplied.intersection({"user", "network", "credential"}):
            marker = ("tainted_file_write_flow", line)
            if marker not in flow_seen:
                result.add_signal(
                    SourceSignal(
                        marker[0],
                        "file_system_access",
                        5,
                        "warning",
                        f"{rel}:{line}",
                        "A bounded static flow links external data to a file write.",
                        "inferred",
                    )
                )
                flow_seen.add(marker)

    ast_outcome = "partial" if exceeded else "completed"
    ast_reason = "ast_node_limit_reached" if exceeded else None
    taint_partial = exceeded or taint_visit_budget[0] <= 0 or taint_name_limit
    taint_reason = (
        "ast_node_limit_reached" if exceeded else
        "taint_visit_limit_reached" if taint_visit_budget[0] <= 0 else
        "taint_name_limit_reached" if taint_name_limit else
        None
    )
    result.records.append(InspectionRecord("python_ast", rel, ast_outcome, ast_reason, len(nodes), MAX_AST_NODES))
    result.records.append(InspectionRecord(
        "taint_tracking",
        rel,
        "partial" if taint_partial else "completed",
        taint_reason,
        MAX_TAINT_NODE_VISITS - taint_visit_budget[0],
        MAX_TAINT_NODE_VISITS,
    ))
    if exceeded or taint_partial:
        result.source_state = "partial"


def _is_versioned_npx(spec: str) -> bool:
    if spec.startswith("@"):
        slash = spec.find("/")
        if slash < 0 or "@" not in spec[slash + 1 :]:
            return False
        version = spec.rsplit("@", 1)[1]
    elif "@" in spec:
        version = spec.rsplit("@", 1)[1]
    else:
        return False
    return re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version) is not None


def _launch_pinning(text: str, rel: str, result: SourceAnalysisResult) -> None:
    findings: set[tuple[str, int, str]] = set()
    npx_re = re.compile(
        r"\bnpx(?:\s+(?:-y|--yes)){0,2}\s+(?P<spec>@[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:@[^\s\"'`;]+)?|[A-Za-z0-9_.-]+(?:@[^\s\"'`;]+)?)",
        re.I,
    )
    uvx_re = re.compile(r"\buvx(?:\s+--?[A-Za-z0-9_.=-]+){0,4}\s+(?P<spec>[A-Za-z0-9_.-]+(?:==[^\s\"'`;]+)?)", re.I)
    pip_re = re.compile(r"\b(?:python\s+-m\s+)?pip\s+install\s+(?P<spec>[^\r\n;&|]+)", re.I)
    docker_re = re.compile(r"\bdocker\s+run(?:\s+--?[A-Za-z0-9_.=-]+){0,8}\s+(?P<image>[A-Za-z0-9./_-]+(?::[A-Za-z0-9_.-]+)?(?:@sha256:[a-fA-F0-9]{64})?)", re.I)

    for line_number, line in enumerate(text.splitlines(), start=1):
        if "mcp" not in line.lower():
            continue
        for match in npx_re.finditer(line):
            spec = match.group("spec")
            if not _is_versioned_npx(spec):
                findings.add(("npx", line_number, spec))
        for match in uvx_re.finditer(line):
            spec = match.group("spec")
            if "==" not in spec:
                findings.add(("uvx", line_number, spec))
        for match in pip_re.finditer(line):
            spec = match.group("spec")
            for token in re.findall(r"[A-Za-z0-9_.-]*mcp[A-Za-z0-9_.-]*(?:==[^\s,]+)?", spec, re.I):
                if "==" not in token:
                    findings.add(("pip", line_number, token))
        for match in docker_re.finditer(line):
            image = match.group("image")
            if "@sha256:" not in image.lower() and (":" not in image.rsplit("/", 1)[-1] or image.lower().endswith(":latest")):
                findings.add(("docker", line_number, image))

    for launcher, line, target in sorted(findings):
        result.add_signal(
            SourceSignal(
                "unpinned_mcp_launch",
                "dependency_risk",
                4,
                "warning",
                f"{rel}:{line}",
                f"MCP launch dependency is not immutably pinned ({launcher}: {target[:120]}).",
            )
        )
    result.records.append(InspectionRecord("mcp_launch_pinning", rel, "completed"))


def analyze_source(source: Path, *, max_files: int, max_bytes: int, max_entries: int) -> SourceAnalysisResult:
    result = SourceAnalysisResult()
    root = source.resolve()
    stack = [root]
    stopped = False

    while stack and not stopped:
        directory = stack.pop()
        try:
            with os.scandir(directory) as directory_entries:
                entries = sorted(directory_entries, key=lambda item: item.name)
        except OSError as exc:
            result.records.append(
                InspectionRecord("source_inventory", _relative(directory, root), "failed", f"scandir:{type(exc).__name__}")
            )
            result.source_state = "partial"
            result.artifact_state = "partial_uninspected_artifacts"
            continue

        for entry in entries:
            result.entries += 1
            entry_path = Path(entry.path)
            rel = _relative(entry_path, root)
            if result.entries > max_entries:
                result.records.append(
                    InspectionRecord("source_inventory", rel, "partial", "entry_limit_reached", result.entries, max_entries)
                )
                result.source_state = "partial"
                result.artifact_state = "partial_uninspected_artifacts"
                stopped = True
                break
            try:
                is_junction = bool(getattr(entry_path, "is_junction", lambda: False)())
            except OSError:
                is_junction = False
            if entry.is_symlink() or is_junction:
                reason = "junction_not_followed" if is_junction else "symlink_not_followed"
                result.records.append(InspectionRecord("source_inventory", rel, "skipped", reason))
                result.artifact_state = "partial_uninspected_artifacts"
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in _EXCLUDED_DIRS:
                        result.records.append(InspectionRecord("source_inventory", rel, "out_of_scope", "excluded_directory"))
                        if entry.name != ".git":
                            result.artifact_state = "partial_uninspected_artifacts"
                    else:
                        stack.append(entry_path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    result.records.append(InspectionRecord("source_inventory", rel, "skipped", "non_regular_file"))
                    result.artifact_state = "partial_uninspected_artifacts"
                    continue
            except OSError as exc:
                result.records.append(InspectionRecord("source_inventory", rel, "failed", f"stat:{type(exc).__name__}"))
                result.artifact_state = "partial_uninspected_artifacts"
                continue

            suffix = entry_path.suffix.lower()
            if suffix in _BYTECODE_SUFFIXES:
                result.records.append(InspectionRecord("artifact_inventory", rel, "partial", "compiled_python_not_decompiled"))
                result.artifact_state = "partial_uninspected_artifacts"
                result.add_signal(
                    SourceSignal(
                        "shipped_python_bytecode",
                        "dependency_risk",
                        6,
                        "warning",
                        rel,
                        "Compiled Python bytecode was present but not decompiled.",
                    )
                )
                continue
            if suffix in _ARCHIVE_SUFFIXES:
                result.records.append(InspectionRecord("artifact_inventory", rel, "partial", "archive_not_expanded"))
                result.artifact_state = "partial_uninspected_artifacts"
                result.add_signal(
                    SourceSignal(
                        "opaque_archive",
                        "dependency_risk",
                        3,
                        "warning",
                        rel,
                        "An archive was inventoried but its contents were not inspected.",
                    )
                )
                continue
            if suffix in _BINARY_SUFFIXES:
                result.records.append(InspectionRecord("artifact_inventory", rel, "partial", "binary_not_inspected"))
                result.artifact_state = "partial_uninspected_artifacts"
                result.add_signal(
                    SourceSignal(
                        "uninspected_executable",
                        "permission_scope",
                        5,
                        "warning",
                        rel,
                        "An executable binary was inventoried but not inspected.",
                    )
                )
                continue

            should_scan_source = suffix in _SOURCE_SUFFIXES
            should_scan_config = suffix in _CONFIG_SUFFIXES or entry_path.name in {"package.json", "requirements.txt"}
            try:
                is_executable = bool(entry.stat(follow_symlinks=False).st_mode & 0o111)
            except OSError:
                is_executable = False
            if is_executable and not should_scan_source:
                result.records.append(InspectionRecord("artifact_inventory", rel, "partial", "unsupported_executable_script"))
                result.artifact_state = "partial_uninspected_artifacts"
                result.add_signal(
                    SourceSignal(
                        "uninspected_executable",
                        "permission_scope",
                        5,
                        "warning",
                        rel,
                        "An executable file used an unsupported source format.",
                    )
                )
                continue
            if not should_scan_source and not should_scan_config:
                if result.artifact_peek_bytes >= MAX_ARTIFACT_PEEK_BYTES:
                    result.records.append(
                        InspectionRecord(
                            "artifact_inventory",
                            rel,
                            "partial",
                            "artifact_peek_budget_reached",
                            result.artifact_peek_bytes,
                            MAX_ARTIFACT_PEEK_BYTES,
                        )
                    )
                    result.artifact_state = "partial_uninspected_artifacts"
                    continue
                try:
                    peek_budget = min(ARTIFACT_PEEK_SIZE, MAX_ARTIFACT_PEEK_BYTES - result.artifact_peek_bytes)
                    with entry_path.open("rb") as stream:
                        prefix = stream.read(peek_budget)
                    result.artifact_peek_bytes += len(prefix)
                except OSError as exc:
                    result.records.append(InspectionRecord("artifact_inventory", rel, "failed", f"peek:{type(exc).__name__}"))
                    result.artifact_state = "partial_uninspected_artifacts"
                    continue
                executable_magic = prefix.startswith(
                    (b"MZ", b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf")
                )
                archive_magic = prefix.startswith(b"PK\x03\x04")
                binary_magic = executable_magic or archive_magic or b"\x00" in prefix
                if binary_magic:
                    result.records.append(InspectionRecord("artifact_inventory", rel, "partial", "opaque_binary_content"))
                    result.artifact_state = "partial_uninspected_artifacts"
                    if executable_magic:
                        result.add_signal(
                            SourceSignal(
                                "concealed_executable_artifact",
                                "permission_scope",
                                5,
                                "warning",
                                rel,
                                "An extensionless or disguised executable artifact was inventoried but not analyzed.",
                            )
                        )
                    elif archive_magic:
                        result.add_signal(
                            SourceSignal(
                                "opaque_archive",
                                "dependency_risk",
                                3,
                                "warning",
                                rel,
                                "An extensionless or disguised archive was inventoried but not expanded.",
                            )
                        )
                    continue
                result.records.append(InspectionRecord("source_inventory", rel, "out_of_scope", "unsupported_text_or_data"))
                continue
            try:
                size = entry_path.stat().st_size
            except OSError as exc:
                result.records.append(InspectionRecord("source_inventory", rel, "failed", f"stat:{type(exc).__name__}"))
                result.source_state = "partial"
                continue
            if result.source_files >= max_files or result.source_bytes + size > max_bytes:
                reason = "file_limit_reached" if result.source_files >= max_files else "byte_limit_reached"
                observed = result.source_files if reason.startswith("file") else result.source_bytes + size
                limit = max_files if reason.startswith("file") else max_bytes
                result.records.append(InspectionRecord("source_patterns", rel, "partial", reason, observed, limit))
                result.source_state = "partial"
                stopped = True
                break
            try:
                remaining_bytes = max_bytes - result.source_bytes
                with entry_path.open("rb") as stream:
                    raw = stream.read(remaining_bytes + 1)
            except OSError as exc:
                result.records.append(InspectionRecord("source_patterns", rel, "failed", f"read:{type(exc).__name__}"))
                result.source_state = "partial"
                continue
            if len(raw) > remaining_bytes:
                result.records.append(InspectionRecord(
                    "source_patterns", rel, "partial", "byte_limit_reached",
                    result.source_bytes + len(raw), max_bytes,
                ))
                result.source_state = "partial"
                stopped = True
                break
            result.source_files += 1
            result.source_bytes += len(raw)
            text = raw.decode("utf-8", errors="replace")

            result.records.append(InspectionRecord("source_inventory", rel, "completed"))
            _launch_pinning(text, rel, result)
            if not should_scan_source:
                result.records.append(InspectionRecord("source_patterns", rel, "out_of_scope", "configuration_only"))
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                for pattern, code, category, points, severity, capabilities in _SOURCE_PATTERNS:
                    if not pattern.search(line):
                        continue
                    result.capabilities.update(capabilities)
                    result.add_signal(
                        SourceSignal(
                            code,
                            category,
                            points,
                            severity,
                            f"{rel}:{line_number}",
                            f"Source contains {code.replace('_', ' ')} behavior.",
                        )
                    )
            result.records.append(InspectionRecord("source_patterns", rel, "completed"))
            if suffix == ".py":
                _python_analysis(text, rel, result)
            else:
                result.records.append(InspectionRecord("python_ast", rel, "out_of_scope", "non_python_source"))
                result.records.append(InspectionRecord("taint_tracking", rel, "out_of_scope", "non_python_source"))

    result.source_scope = (
        f"{result.source_files} bounded source/config files, {result.source_bytes} bytes, "
        f"{result.entries} filesystem entries; symlinks were not followed"
    )
    return result
