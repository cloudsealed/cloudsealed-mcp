#!/usr/bin/env python3
"""MCP server for the CloudSealed analysis engines.

Exposes two independent, stateless analysis tools to MCP clients (Claude
Code, Cursor, Claude Desktop, etc.):

- cloudsealed_analyze_billing_waste: cost anomaly detection over a cloud
  billing export, using the ``cloudsealed-jit`` library directly (no network
  call — pure computation).
- cloudsealed_score_architecture_risk: deterministic architecture risk
  scoring from a declared system inventory, delegating to the
  Predictive-ML-Core HTTP service running in production.

Both tools are read-only and side-effect-free: they never write files or
call third parties other than the Predictive-ML-Core API itself.
"""

from __future__ import annotations

import json
import os
from enum import Enum
from typing import Any, Optional

import httpx
from cloudsealed_jit import analyze
from cloudsealed_jit.parsing import ParseError, parse_billing_csv
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

mcp = MCPServer("cloudsealed_mcp")

#: Defaults to a locally self-hosted instance — the CloudSealed production
#: deployment requires an X-Api-Key this server has no way to distribute.
#: Start one with `docker run -p 8092:8092 cloudsealed/predictive-ml-core`,
#: or point PREDICTIVE_ML_CORE_URL at your own deployment.
PREDICTIVE_ML_CORE_URL = os.getenv("PREDICTIVE_ML_CORE_URL", "http://localhost:8092")
PREDICTIVE_ML_CORE_API_KEY = os.getenv("PREDICTIVE_ML_CORE_API_KEY", "")


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


# --------------------------------------------------------------------------
# cloudsealed_analyze_billing_waste
# --------------------------------------------------------------------------


class AnalysisType(str, Enum):
    """Analysis mode for the billing waste audit."""

    WASTE_AUDIT = "waste-audit"
    COST_FORECAST = "cost-forecast"
    EFFICIENCY = "efficiency"


class AnalyzeBillingWasteInput(BaseModel):
    """Input model for the billing waste analysis tool."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    csv_content: str = Field(
        ...,
        description=(
            "Raw text contents of a cloud billing export CSV. Supports AWS Cost and "
            "Usage Report (lineItem/UsageStartDate, lineItem/UnblendedCost), GCP "
            "billing export (usage_start_time, cost), Azure cost export "
            "(Date/UsageDateTime, Cost/CostInBillingCurrency), and a generic "
            "date+cost heuristic for anything else."
        ),
        min_length=1,
    )
    analysis_type: AnalysisType = Field(
        default=AnalysisType.WASTE_AUDIT,
        description="'waste-audit' (default) finds cost anomalies and savings recommendations; "
        "'cost-forecast' adds a 30-day run-rate projection; 'efficiency' is an alias "
        "of waste-audit tuned for the same output shape.",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="Output format: 'markdown' or 'json'."
    )


def _render_billing_waste_markdown(result: Any) -> str:
    metrics = result.metrics
    lines = [
        "# Billing waste audit",
        "",
        result.summary,
        "",
        f"- **Waste**: {metrics.wastePercentage:.1f}%",
        f"- **Avg daily cost**: {metrics.averageDailyCost:,.2f}",
        f"- **Stability ratio**: {metrics.sharpeRatio:.2f}",
        f"- **Anomalies found**: {len(result.anomalies)}",
    ]
    if result.anomalies:
        lines += ["", "## Anomalies", ""]
        for a in result.anomalies:
            lines.append(
                f"- `{a.date}` **{a.severity}** — expected {a.expectedCost:,.2f}, "
                f"actual {a.actualCost:,.2f} ({a.deviation:+.1f}%, z={a.zScore:.1f}): {a.description}"
            )
    if result.recommendations:
        lines += ["", "## Recommendations", ""]
        for r in result.recommendations:
            lines.append(f"- **{r.title}** ({r.effort} effort, {r.potentialSavings:,.2f}/30d) — {r.description}")
    return "\n".join(lines)


@mcp.tool(
    name="cloudsealed_analyze_billing_waste",
    annotations=ToolAnnotations(
        title="Analyze Cloud Billing Waste",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def cloudsealed_analyze_billing_waste(params: AnalyzeBillingWasteInput) -> str:
    """Detect cost anomalies in a cloud billing export (AWS/GCP/Azure/generic).

    Models the expected daily spend for each day as a rolling-median baseline
    times a day-of-week factor, then flags days whose actual spend deviates
    from that baseline by a robust (median-absolute-deviation-based) modified
    z-score. This is resistant to the "masking effect" that causes textbook
    mean+standard-deviation detectors to miss anomalies once a few large
    spikes have inflated the standard deviation. It does NOT call any cloud
    provider API — the caller must already have exported the billing data to
    a CSV/text string and pass its contents directly.

    Args:
        params (AnalyzeBillingWasteInput): Validated input containing:
            - csv_content (str): Raw billing export text (see field description
              for supported provider formats).
            - analysis_type (AnalysisType): 'waste-audit' (default),
              'cost-forecast', or 'efficiency'.
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Markdown report, or a JSON object with this schema:
        {
            "anomalies": [
                {"date": str, "expectedCost": float, "actualCost": float,
                 "deviation": float, "zScore": float,
                 "severity": "LOW"|"MEDIUM"|"HIGH"|"CRITICAL", "description": str}
            ],
            "metrics": {"averageDailyCost": float, "stdDeviation": float,
                        "sharpeRatio": float, "wastePercentage": float},
            "recommendations": [
                {"title": str, "description": str, "potentialSavings": float,
                 "effort": "LOW"|"MEDIUM"|"HIGH"}
            ],
            "summary": str
        }

        Error response: "Error: <message>" when the CSV cannot be parsed
        (e.g. no recognizable date/cost columns).

    Examples:
        - Use when: "Why did our AWS bill spike last month?" -> paste the CUR
          export contents as csv_content.
        - Use when: "What will we spend next month at this rate?" ->
          analysis_type="cost-forecast".
        - Don't use when: you need architecture/reliability risk instead of
          cost — use cloudsealed_score_architecture_risk.
    """
    try:
        series = parse_billing_csv(params.csv_content)
    except ParseError as exc:
        return f"Error: {exc}"

    result = analyze(series, params.analysis_type.value)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(result.to_dict(), indent=2)
    return _render_billing_waste_markdown(result)


# --------------------------------------------------------------------------
# cloudsealed_score_architecture_risk
# --------------------------------------------------------------------------


class SystemType(str, Enum):
    APPLICATION = "APPLICATION"
    DATABASE = "DATABASE"
    API = "API"
    THIRD_PARTY_SERVICE = "THIRD_PARTY_SERVICE"


class Criticality(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class SystemInput(BaseModel):
    """One system in the declared architecture inventory."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., description="System name, e.g. 'checkout-api'.", min_length=1, max_length=200)
    type: SystemType = Field(..., description="System type.")
    criticality: Criticality = Field(..., description="Business criticality of this system.")
    public_facing: bool = Field(default=False, description="Whether this system is reachable from the public internet.")
    data_sensitivity: Optional[str] = Field(
        default=None, description="Free-text sensitivity label, e.g. 'HIGH', 'PII'. Optional."
    )
    auth_method: Optional[str] = Field(
        default=None, description="Auth mechanism, e.g. 'OAUTH2', 'MTLS', 'SSO'. Optional."
    )


class HistoricalMetricsInput(BaseModel):
    """Optional observed latency/throughput metrics improving the scalability-gap score."""

    model_config = ConfigDict(extra="forbid")

    avg_latency_ms: Optional[float] = Field(default=None, ge=0)
    p99_latency_ms: Optional[float] = Field(default=None, ge=0)
    requests_per_second: Optional[float] = Field(default=None, ge=0)


class ScoreArchitectureRiskInput(BaseModel):
    """Input model for the architecture risk scoring tool."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    company_name: str = Field(..., description="Name of the company/project being assessed.", min_length=1, max_length=200)
    systems: list[SystemInput] = Field(
        ..., description="The declared system inventory to score. At least one system is required.", min_length=1, max_length=200
    )
    historical_metrics: Optional[HistoricalMetricsInput] = Field(
        default=None,
        description="Optional observed latency/throughput data. Without it, the "
        "scalability-gap dimension falls back to a weaker, explicitly conditional signal.",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="Output format: 'markdown' or 'json'."
    )


def _to_wire_system(system: SystemInput) -> dict:
    return {
        "name": system.name,
        "type": system.type.value,
        "criticality": system.criticality.value,
        "publicFacing": system.public_facing,
        "dataSensitivity": system.data_sensitivity,
        "authMethod": system.auth_method,
    }


def _render_architecture_risk_markdown(report: dict) -> str:
    predictions = report["predictions"]
    lines = [
        "# Architecture risk assessment",
        "",
        report["architectureSummary"],
        "",
        f"- **Overall score**: {report['overallArchitectureScore']}/100",
        f"- **Systems assessed**: {len(predictions)}",
    ]
    for prediction in predictions:
        risk = prediction["riskScores"]
        lines += [
            "",
            f"## {prediction['systemName']}",
            f"SPOF={risk['singlePointOfFailure']} "
            f"Coupling={risk['excessiveCoupling']} "
            f"ScalabilityGap={risk['scalabilityGap']}",
        ]
        for finding in prediction["findings"]:
            lines.append(f"- **{finding['severity']}** {finding['title']} — {finding['description']}")
            lines.append(f"  - Remediation: {finding['remediation']}")
        for rec in prediction["recommendations"]:
            lines.append(f"- Recommendation ({rec['effort']}): {rec['title']} — {rec['description']}")
    return "\n".join(lines)


@mcp.tool(
    name="cloudsealed_score_architecture_risk",
    annotations=ToolAnnotations(
        title="Score Architecture Risk",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def cloudsealed_score_architecture_risk(params: ScoreArchitectureRiskInput) -> str:
    """Score architecture risk from a declared system inventory.

    Scores single-point-of-failure, excessive-coupling, and scalability-gap
    risk (0-100 each) for every declared system using explicit, weighted
    rules — not a trained model. Every score ships with a rule-by-rule
    breakdown so the reasoning is auditable, not a black box. Calls the
    Predictive-ML-Core production HTTP service (or a self-hosted instance if
    PREDICTIVE_ML_CORE_URL is set).

    Args:
        params (ScoreArchitectureRiskInput): Validated input containing:
            - company_name (str): Name of the company/project.
            - systems (list[SystemInput]): Declared inventory — each with
              name, type (APPLICATION|DATABASE|API|THIRD_PARTY_SERVICE),
              criticality (LOW|MEDIUM|HIGH|CRITICAL), public_facing,
              and optional data_sensitivity/auth_method.
            - historical_metrics (Optional[HistoricalMetricsInput]): Observed
              latency/throughput, improves the scalability-gap score.
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Markdown report, or a JSON object with this schema:
        {
            "predictions": [
                {"systemName": str,
                 "riskScores": {"singlePointOfFailure": int, "excessiveCoupling": int, "scalabilityGap": int},
                 "scoreBreakdown": {...rule-by-rule points and rationale...},
                 "findings": [{"title": str, "severity": str, "description": str, "remediation": str}],
                 "recommendations": [{"title": str, "description": str, "effort": str}]}
            ],
            "architectureSummary": str,
            "overallArchitectureScore": int
        }

        Error response: "Error: <message>" if the service is unreachable or
        rejects the request (e.g. empty systems list).

    Examples:
        - Use when: "Is our checkout service a single point of failure?" ->
          declare it with criticality=CRITICAL, type=API.
        - Use when: "Which of these services should we harden first?" ->
          declare the whole inventory and compare riskScores.
        - Don't use when: you need cost/billing analysis — use
          cloudsealed_analyze_billing_waste.

    Error Handling:
        - Returns "Error: Request timed out..." if the service doesn't
          respond within 30s.
        - Returns "Error: ..." with the upstream message on 4xx/5xx responses.
    """
    payload = {
        "companyName": params.company_name,
        "systems": [_to_wire_system(s) for s in params.systems],
    }
    if params.historical_metrics is not None:
        payload["historicalMetrics"] = {
            "avgLatencyMs": params.historical_metrics.avg_latency_ms,
            "p99LatencyMs": params.historical_metrics.p99_latency_ms,
            "requestsPerSecond": params.historical_metrics.requests_per_second,
        }

    headers = {"Content-Type": "application/json"}
    if PREDICTIVE_ML_CORE_API_KEY:
        headers["X-Api-Key"] = PREDICTIVE_ML_CORE_API_KEY

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{PREDICTIVE_ML_CORE_URL}/v1/predict-architecture",
                json=payload,
                headers=headers,
                timeout=30.0,
            )
            response.raise_for_status()
            report = response.json()
    except httpx.TimeoutException:
        return "Error: Request timed out. The Predictive-ML-Core service may be cold-starting; try again."
    except httpx.HTTPStatusError as exc:
        return f"Error: Predictive-ML-Core rejected the request ({exc.response.status_code}): {exc.response.text}"
    except httpx.HTTPError as exc:
        return (
            f"Error: Could not reach Predictive-ML-Core at {PREDICTIVE_ML_CORE_URL}: {exc}. "
            "Start a local instance with `docker run -p 8092:8092 cloudsealed/predictive-ml-core`, "
            "or set PREDICTIVE_ML_CORE_URL to a reachable deployment."
        )

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(report, indent=2)
    return _render_architecture_risk_markdown(report)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
