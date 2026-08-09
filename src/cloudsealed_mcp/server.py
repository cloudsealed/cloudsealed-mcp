#!/usr/bin/env python3
"""MCP server for the CloudSealed analysis engines.

Exposes three independent, stateless analysis tools to MCP clients (Claude
Code, Cursor, Claude Desktop, etc.):

- cloudsealed_analyze_billing_waste: cost anomaly detection over a cloud
  billing export, using the ``cloudsealed-jit`` library directly (no network
  call — pure computation).
- cloudsealed_score_architecture_risk: deterministic architecture risk
  scoring from a declared system inventory, delegating to the
  Predictive-ML-Core HTTP service.
- cloudsealed_correlate_cost_and_risk: runs both engines and cross-references
  them — surfaces systems that are both costly and high architecture risk
  ("double jeopardy"). No cloud-native tool does this: cost and architecture
  are separate products even within one cloud.

All tools are read-only and side-effect-free: they never write files or
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
            "(Date/UsageDateTime, Cost/CostInBillingCurrency), the vendor-neutral "
            "FOCUS 1.0 format (ChargePeriodStart, BilledCost, ServiceName), and a "
            "generic date+cost heuristic for anything else."
        ),
        min_length=1,
    )
    analysis_type: AnalysisType = Field(
        default=AnalysisType.WASTE_AUDIT,
        description="'waste-audit' (default) finds cost anomalies and savings recommendations; "
        "'cost-forecast' adds a trend-aware 30-day projection; 'efficiency' is an alias "
        "of waste-audit tuned for the same output shape.",
    )
    budget: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional monthly budget. If set, the result forecasts when the current "
        "spend trend crosses it (proactive budget-breach prediction).",
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

    result = analyze(series, params.analysis_type.value, budget=params.budget)

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


async def _score_architecture(
    company_name: str,
    systems: list[SystemInput],
    historical_metrics: Optional[HistoricalMetricsInput] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """Call Predictive-ML-Core. Returns (report, None) or (None, error_message).

    Shared by cloudsealed_score_architecture_risk and
    cloudsealed_correlate_cost_and_risk so the HTTP handling lives in one place.
    """
    payload: dict[str, Any] = {
        "companyName": company_name,
        "systems": [_to_wire_system(s) for s in systems],
    }
    if historical_metrics is not None:
        payload["historicalMetrics"] = {
            "avgLatencyMs": historical_metrics.avg_latency_ms,
            "p99LatencyMs": historical_metrics.p99_latency_ms,
            "requestsPerSecond": historical_metrics.requests_per_second,
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
            return response.json(), None
    except httpx.TimeoutException:
        return None, "Error: Request timed out. The Predictive-ML-Core service may be cold-starting; try again."
    except httpx.HTTPStatusError as exc:
        return None, f"Error: Predictive-ML-Core rejected the request ({exc.response.status_code}): {exc.response.text}"
    except httpx.HTTPError as exc:
        return None, (
            f"Error: Could not reach Predictive-ML-Core at {PREDICTIVE_ML_CORE_URL}: {exc}. "
            "Start a local instance with `docker run -p 8092:8092 cloudsealed/predictive-ml-core`, "
            "or set PREDICTIVE_ML_CORE_URL to a reachable deployment."
        )


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
    report, error = await _score_architecture(
        params.company_name, params.systems, params.historical_metrics
    )
    if error is not None:
        return error
    assert report is not None

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(report, indent=2)
    return _render_architecture_risk_markdown(report)


# --------------------------------------------------------------------------
# cloudsealed_correlate_cost_and_risk
# --------------------------------------------------------------------------


class CorrelateCostAndRiskInput(BaseModel):
    """Input model for the combined cost + architecture-risk tool."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    company_name: str = Field(..., description="Name of the company/project being assessed.", min_length=1, max_length=200)
    csv_content: str = Field(
        ...,
        description="Raw text contents of a cloud billing export CSV (AWS/GCP/Azure/generic), "
        "same formats as cloudsealed_analyze_billing_waste.",
        min_length=1,
    )
    systems: list[SystemInput] = Field(
        ...,
        description="The declared system inventory to score. To get the cross-referenced "
        "'double jeopardy' view, name systems to match the service/product names in the billing "
        "export (e.g. a system named 'EC2' or 'checkout-api' matched against a billing service "
        "'Amazon EC2'). Matching is a case-insensitive substring heuristic.",
        min_length=1,
        max_length=200,
    )
    historical_metrics: Optional[HistoricalMetricsInput] = Field(
        default=None, description="Optional observed latency/throughput data, improves the scalability-gap score."
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="Output format: 'markdown' or 'json'."
    )


def _normalize(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _match_service(system_name: str, service_names: list[str]) -> Optional[str]:
    """Heuristic name match between a declared system and a billing service.

    Case-insensitive, alphanumeric-only substring match in either direction.
    Returns the matched service name, or None.
    """
    sys_norm = _normalize(system_name)
    if not sys_norm:
        return None
    for service in service_names:
        svc_norm = _normalize(service)
        if not svc_norm:
            continue
        if sys_norm in svc_norm or svc_norm in sys_norm:
            return service
    return None


def _build_correlation(billing_result: Any, service_totals: dict[str, float], risk_report: dict) -> dict:
    """Cross-reference per-service cost against per-system architecture risk.

    'Double jeopardy' = a system that is both expensive (high share of spend or
    on an anomalous service) AND high architecture risk. This intersection is
    the CloudSealed-specific view: no single cloud-native tool correlates a cost
    anomaly with the reliability risk of the system incurring it.
    """
    total_spend = sum(service_totals.values()) or 1.0
    predictions = {p["systemName"]: p for p in risk_report["predictions"]}
    service_names = list(service_totals.keys())

    correlated = []
    for system_name, prediction in predictions.items():
        risk = prediction["riskScores"]
        max_risk = max(risk["singlePointOfFailure"], risk["excessiveCoupling"], risk["scalabilityGap"])
        matched_service = _match_service(system_name, service_names)
        service_cost = service_totals.get(matched_service, 0.0) if matched_service else 0.0
        cost_share = service_cost / total_spend * 100.0

        # Double jeopardy score: high when a system is both costly and risky.
        # Normalised to 0-100; cost_share is 0-100, max_risk is 0-100.
        double_jeopardy = round((cost_share * max_risk) / 100.0, 1)

        correlated.append({
            "systemName": system_name,
            "maxRiskScore": max_risk,
            "matchedBillingService": matched_service,
            "serviceCost": round(service_cost, 2),
            "costSharePercent": round(cost_share, 1),
            "doubleJeopardyScore": double_jeopardy,
        })

    correlated.sort(key=lambda c: c["doubleJeopardyScore"], reverse=True)
    return {
        "totalSpend": round(total_spend, 2),
        "wastePercentage": billing_result.metrics.wastePercentage,
        "anomalyCount": len(billing_result.anomalies),
        "overallArchitectureScore": risk_report["overallArchitectureScore"],
        "correlations": correlated,
        "unmatchedNote": (
            "Systems with matchedBillingService=null could not be linked to a billing "
            "service by name. To get the full cross-referenced view, name systems to match "
            "the service/product names in the billing export."
            if any(c["matchedBillingService"] is None for c in correlated)
            else ""
        ),
    }


def _render_correlation_markdown(corr: dict) -> str:
    lines = [
        "# Cost + architecture-risk correlation",
        "",
        f"- **Total spend**: {corr['totalSpend']:,.2f}",
        f"- **Waste**: {corr['wastePercentage']:.1f}%  |  **Anomalies**: {corr['anomalyCount']}",
        f"- **Overall architecture score**: {corr['overallArchitectureScore']}/100",
        "",
        "## Double jeopardy — costly *and* fragile",
        "",
        "Systems ranked by the intersection of spend share and architecture risk. "
        "A high score means you are spending a lot on something that is also a "
        "reliability risk — the priority-one place to act.",
        "",
        "| System | Max risk | Matched service | Cost share | Double-jeopardy |",
        "|---|---|---|---|---|",
    ]
    for c in corr["correlations"]:
        service = c["matchedBillingService"] or "—"
        lines.append(
            f"| {c['systemName']} | {c['maxRiskScore']} | {service} | "
            f"{c['costSharePercent']:.1f}% | **{c['doubleJeopardyScore']}** |"
        )
    if corr["unmatchedNote"]:
        lines += ["", f"> {corr['unmatchedNote']}"]
    return "\n".join(lines)


@mcp.tool(
    name="cloudsealed_correlate_cost_and_risk",
    annotations=ToolAnnotations(
        title="Correlate Cost Anomalies with Architecture Risk",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def cloudsealed_correlate_cost_and_risk(params: CorrelateCostAndRiskInput) -> str:
    """Cross-reference cloud cost anomalies with architecture risk in one pass.

    Runs both CloudSealed engines and correlates them: it finds systems that are
    both expensive (high share of spend) AND high architecture risk — the
    "double jeopardy" cases where you are spending heavily on something that is
    also fragile. This intersection is not available from any single cloud-native
    tool: AWS Cost Anomaly Detection does not know your architecture risk, and
    the Well-Architected Tool does not know your cost anomalies — they are
    separate products even within one cloud. This tool is vendor-neutral and runs
    on data you already exported.

    Args:
        params (CorrelateCostAndRiskInput): Validated input containing:
            - company_name (str): Name of the company/project.
            - csv_content (str): Raw billing export text (AWS/GCP/Azure/generic).
            - systems (list[SystemInput]): Declared inventory. Name systems to
              match billing service/product names to get the linked view.
            - historical_metrics (Optional[HistoricalMetricsInput]): improves
              the scalability-gap score.
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Markdown report, or a JSON object with this schema:
        {
            "totalSpend": float,
            "wastePercentage": float,
            "anomalyCount": int,
            "overallArchitectureScore": int,
            "correlations": [
                {"systemName": str, "maxRiskScore": int,
                 "matchedBillingService": str | null, "serviceCost": float,
                 "costSharePercent": float, "doubleJeopardyScore": float}
            ],
            "unmatchedNote": str
        }

        Error response: "Error: <message>" if the CSV cannot be parsed or the
        Predictive-ML-Core service is unreachable.

    Examples:
        - Use when: "Which of our services is both burning money and a reliability
          risk?" -> pass the billing CSV and the system inventory.
        - Use when: "Prioritize our cloud remediation work by cost AND risk
          together, not one at a time."
        - Don't use when: you only need cost (use cloudsealed_analyze_billing_waste)
          or only need risk (use cloudsealed_score_architecture_risk).
    """
    try:
        series = parse_billing_csv(params.csv_content)
    except ParseError as exc:
        return f"Error: {exc}"

    billing_result = analyze(series, "waste-audit")
    service_totals = {name: sum(costs) for name, costs in series.by_service.items()}

    risk_report, error = await _score_architecture(
        params.company_name, params.systems, params.historical_metrics
    )
    if error is not None:
        return error
    assert risk_report is not None

    correlation = _build_correlation(billing_result, service_totals, risk_report)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(correlation, indent=2)
    return _render_correlation_markdown(correlation)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
