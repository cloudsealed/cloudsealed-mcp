# cloudsealed-mcp

MCP server that gives AI agents (Claude Code, Claude Desktop, Cursor, etc.)
direct access to three deterministic CloudSealed analysis tools:

- **`cloudsealed_analyze_billing_waste`** — cost anomaly detection over a
  cloud billing export (AWS/GCP/Azure/generic), using
  [`cloudsealed-jit`](https://github.com/cloudsealed/JIT-Optimization-Engine)'s
  rolling-median + MAD baseline. Runs locally, no network call.
- **`cloudsealed_score_architecture_risk`** — deterministic, auditable
  architecture risk scoring (single point of failure, excessive coupling,
  scalability gap) from a declared system inventory, backed by
  [`Predictive-ML-Core`](https://github.com/cloudsealed/Predictive-ML-Core).
- **`cloudsealed_correlate_cost_and_risk`** — runs both engines and
  cross-references them, ranking systems that are **both** costly **and** high
  architecture risk ("double jeopardy"). No cloud-native tool does this: cost
  anomaly detection and architecture review are separate products even within
  one cloud, so nothing tells you "this cost spike is on a system that's also a
  single point of failure." Vendor-neutral, runs on data you already exported.

All tools are read-only: they never write files, and the only network call any
of them makes is to the Predictive-ML-Core service you point it at.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

<!-- mcp-name: io.github.cloudsealed/cloudsealed-mcp -->

---

## Install

```bash
# recommended: no local install, uvx fetches and runs it on demand
uvx cloudsealed-mcp

# or, from source until this is published to PyPI
pip install git+https://github.com/cloudsealed/cloudsealed-mcp
```

## Configure your MCP client

Add to your client's MCP config (`.mcp.json` for Claude Code,
`claude_desktop_config.json` for Claude Desktop, Cursor's MCP settings, etc.):

```json
{
  "mcpServers": {
    "cloudsealed": {
      "command": "uvx",
      "args": ["cloudsealed-mcp"]
    }
  }
}
```

Restart the client, and both tools become available to the agent.

## `cloudsealed_score_architecture_risk` needs a running Predictive-ML-Core

`cloudsealed_analyze_billing_waste` works out of the box — the analysis
engine is a pure Python dependency, no server involved.

`cloudsealed_score_architecture_risk` calls the Predictive-ML-Core HTTP API.
By default it looks for one at `http://localhost:8092`. Start one with:

```bash
docker run -p 8092:8092 cloudsealed/predictive-ml-core
```

To point at a different deployment (self-hosted or otherwise), set:

```bash
export PREDICTIVE_ML_CORE_URL="https://your-deployment"
export PREDICTIVE_ML_CORE_API_KEY="..."   # only if that deployment requires one
```

## Example prompts

- *"Here's our AWS Cost and Usage Report for last month — find the cost
  anomalies and tell me what to fix first."* (paste the CSV; the agent calls
  `cloudsealed_analyze_billing_waste`)
- *"We have a checkout-api (CRITICAL, public-facing, no declared auth), an
  orders-db (CRITICAL), and a third-party payment-gateway. What's our
  biggest architecture risk?"* (the agent calls
  `cloudsealed_score_architecture_risk`)
- *"Here's our billing export and our system inventory — which service is both
  burning money and a reliability risk? Prioritize our cloud work by cost AND
  risk together."* (the agent calls `cloudsealed_correlate_cost_and_risk`; name
  systems to match billing service names to get the linked view)

## Why deterministic engines, not another LLM call

Both underlying engines score with explicit, auditable rules — not a model.
Every anomaly and every risk score traces back to a specific rule and a
stated rationale (see
[JIT's METHODOLOGY.md](https://github.com/cloudsealed/JIT-Optimization-Engine/blob/main/METHODOLOGY.md)
and
[Predictive-ML-Core's METHODOLOGY.md](https://github.com/cloudsealed/Predictive-ML-Core/blob/main/METHODOLOGY.md)).
That means an agent calling these tools gets a reproducible, explainable
answer instead of a second opinion from another LLM.

## FAQ

**What's an MCP server, and why not just ask the agent to write the analysis
code itself?** MCP (Model Context Protocol) lets an agent call a real tool
instead of generating code from scratch each time. The underlying engines
here are deterministic and already tested/benchmarked — an agent calling
them gets a reproducible answer, not a fresh guess with its own bugs.

**Which clients support this?** Any MCP client: Claude Code, Claude Desktop,
Cursor, Windsurf, and others that read a standard `mcpServers` config block.

**Is there a hosted version, or do I need to run anything locally?**
`cloudsealed_analyze_billing_waste` needs nothing but the `uvx` invocation.
`cloudsealed_score_architecture_risk` needs a Predictive-ML-Core instance
reachable at `PREDICTIVE_ML_CORE_URL` — self-hosted via Docker by default
(see above), or your own deployment.

**Does this send my billing data anywhere?** No — the billing analysis runs
entirely in-process. Only the architecture tool makes a network call, and
only to whichever `PREDICTIVE_ML_CORE_URL` you configure.

## Development

```bash
pip install -e ".[dev]"
python -m py_compile src/cloudsealed_mcp/server.py
```

## License

MIT. See [LICENSE](LICENSE).
