# Experimental MCP risk scanner

This package is deliberately **not connected to the Campfire engine, desktop UI, or its existing `/mcp` endpoint**. It is a local prototype on `feature/mcp-risk-scanner-local`; no target package is installed or launched by the scanner.

## What it does

- Scores separate evidence categories with fixed budgets totaling 100 risk points. `securityScore = 100 - riskScore`; the score is not a probability of compromise. Critical evidence overrides the numeric verdict. Missing coverage is shown, not counted as a clean result.
- Checks supplied `tools/list` snapshots (names, descriptions, input/output schemas, annotations and metadata), optional local source/manifest heuristics, and explicit authorization scopes. Source checks are *potential capability* findings, not proof that code took an action. Dependency checks cover pinning/install scripts, **not CVEs**.
- Stores a user-saved SHA-256 reference fingerprint of each complete tool definition. Changed, added, or removed tools appear as findings with a risk score; the reference is never silently updated. Saving a reference is **not** an approval or safety certification.
- Can query **only an already-running MCP endpoint on numeric loopback** (`127.0.0.1` or `[::1]`) after `--confirm-connect`. Redirects and ambient proxy settings are disabled. `stdio` commands, arbitrary public URLs, OAuth credentials, and target installation are not accepted.
- Includes an optional **observation-only** stdio MCP relay that re-reads `tools/list` before each forwarded call, inspects arguments and text results, and writes redacted JSONL observations. It does **not** block, modify, or sanitize tool calls or results. A dangerous result still reaches the client; the relay is not a security boundary. It sees MCP messages, **not the target process's private filesystem, network traffic, or environment access**. Tool invocation itself may have side effects, so use only a target you have chosen to run.
- Prints a human-readable score, category penalties, coverage, and evidence by default; `--json` is available for machines.
- Optional Solar Pro 3 review sends redacted tool definitions only with both `--llm` and `--consent-cloud` plus `UPSTAGE_API_KEY`. Its suggestions request manual review; they do not set numeric points. No cloud call is made by default.

## Run locally

From the repository root with Python 3.10+ and `mcp>=1.2,<2`, `httpx`, and `uvicorn` available:

```powershell
python -m experiments.mcp_risk_scanner.cli scan experiments/mcp_risk_scanner/fixtures/benign_tools.json --server-id demo
python -m experiments.mcp_risk_scanner.cli scan experiments/mcp_risk_scanner/fixtures/poisoned_tools.json --server-id demo
python -m unittest discover -s experiments/mcp_risk_scanner/tests -q
```

For an MCP server **already running** at `http://127.0.0.1:PORT/mcp`:

```powershell
python -m experiments.mcp_risk_scanner.cli probe http://127.0.0.1:PORT/mcp --confirm-connect
python -m experiments.mcp_risk_scanner.cli probe http://127.0.0.1:PORT/mcp --confirm-connect --save-baseline .\reference-tools.json
python -m experiments.mcp_risk_scanner.cli probe http://127.0.0.1:PORT/mcp --confirm-connect --baseline .\reference-tools.json
python -m experiments.mcp_risk_scanner.cli proxy http://127.0.0.1:PORT/mcp --confirm-connect --baseline .\reference-tools.json --audit-file .\mcp-audit.jsonl
```

The relay speaks MCP over stdin/stdout and is meant to be started by a separate MCP client. To include its observations in a later assessment, use `probe ... --runtime-audit .\mcp-audit.jsonl` or `scan ... --runtime-audit ...`. These local files can contain identifying tool names; keep them private. A changed definition is reported but **does not stop forwarding**; a change may be a legitimate update. The relay can also run without `--baseline`, but then it cannot detect definition drift.

## Important limits before production

This prototype does not inspect private target OS behavior, detect all prompt injections, verify closed-source servers, query vulnerability feeds, authenticate remote servers, or safely fetch arbitrary internet MCP URLs. Source and dependency findings are heuristic. Metadata can be benign while backend code changes. A message-level proxy cannot establish that a target did not access secrets or send data through its own network connection. Production use needs isolated execution and OS/network observation, authenticated registration and audit integrity, additional MCP transport/version coverage, and a calibrated test corpus before any score is treated as a policy decision.
