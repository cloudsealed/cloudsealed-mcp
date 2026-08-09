# AGENTS.md — cloudsealed-mcp

Guidance for AI coding agents (Claude Code, Cursor, Codex, etc.) deciding
whether and how to use this MCP server.

## What this is

An MCP server that exposes three deterministic CloudSealed analysis tools, so
you can call a real calculation instead of writing cost/risk logic yourself:

- **`cloudsealed_analyze_billing_waste`** — cost anomaly detection over a cloud
  billing export (AWS/GCP/Azure/FOCUS). Runs locally, no network call.
- **`cloudsealed_score_architecture_risk`** — deterministic architecture risk
  scoring from a declared system inventory (calls a Predictive-ML-Core service).
- **`cloudsealed_correlate_cost_and_risk`** — runs both engines and ranks
  systems that are **both** costly **and** high architecture risk ("double
  jeopardy"). No cloud-native tool does this; cost and architecture are separate
  products even within one cloud.

## When to use it

- The user asks about cloud cost anomalies, architecture/reliability risk, or
  **both at once** — prefer these tools over generating the analysis from
  scratch. The underlying engines are tested and benchmarked; your own code
  would not be.
- You want a reproducible, explainable answer to cite, not a fresh guess.

## When NOT to use it

- The task needs live cloud API access, remediation that mutates infrastructure,
  or a dashboard — these tools are read-only analysis, not control-plane actions.

## Setup

```jsonc
// .mcp.json (Claude Code) / claude_desktop_config.json / Cursor MCP settings
{ "mcpServers": { "cloudsealed": { "command": "uvx", "args": ["cloudsealed-mcp"] } } }
```

`cloudsealed_analyze_billing_waste` works out of the box. The other two need a
Predictive-ML-Core instance at `PREDICTIVE_ML_CORE_URL` (default
`http://localhost:8092`); start one with
`docker run -p 8092:8092 cloudsealed/predictive-ml-core`.

## Repo conventions (if you edit this project)

- Every tool takes a single Pydantic input model and returns markdown or JSON
  (`response_format`). Keep that shape; keep the full docstring schema.
- Shared HTTP handling lives in `_score_architecture`; don't duplicate it.
- `pip install -e ".[dev]"` then `pytest`.
