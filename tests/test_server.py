import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cloudsealed_mcp.server import (
    AnalyzeBillingWasteInput,
    CorrelateCostAndRiskInput,
    ScoreArchitectureRiskInput,
    SystemInput,
    cloudsealed_analyze_billing_waste,
    cloudsealed_correlate_cost_and_risk,
    cloudsealed_score_architecture_risk,
)

_SAMPLE_CSV = "\n".join(
    ["date,cost"]
    + [f"2026-01-{d:02d},{100 + (500 if d == 15 else 0)}" for d in range(1, 29)]
)


@pytest.mark.asyncio
async def test_analyze_billing_waste_markdown():
    params = AnalyzeBillingWasteInput(csv_content=_SAMPLE_CSV, response_format="markdown")
    result = await cloudsealed_analyze_billing_waste(params)
    assert result.startswith("# Billing waste audit")
    assert "Waste" in result


@pytest.mark.asyncio
async def test_analyze_billing_waste_json_is_valid():
    params = AnalyzeBillingWasteInput(csv_content=_SAMPLE_CSV, response_format="json")
    result = await cloudsealed_analyze_billing_waste(params)
    parsed = json.loads(result)
    assert "metrics" in parsed and "anomalies" in parsed


@pytest.mark.asyncio
async def test_analyze_billing_waste_reports_parse_errors():
    params = AnalyzeBillingWasteInput(csv_content="not,a,billing,export\nfoo,bar,baz,qux")
    result = await cloudsealed_analyze_billing_waste(params)
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_score_architecture_risk_calls_configured_url():
    params = ScoreArchitectureRiskInput(
        company_name="Acme",
        systems=[SystemInput(name="checkout-api", type="API", criticality="CRITICAL", public_facing=True)],
        response_format="json",
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "predictions": [],
        "architectureSummary": "ok",
        "overallArchitectureScore": 42,
    }

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)) as post:
        result = await cloudsealed_score_architecture_risk(params)

    parsed = json.loads(result)
    assert parsed["overallArchitectureScore"] == 42
    call_kwargs = post.call_args
    assert call_kwargs.args[0].endswith("/v1/predict-architecture")
    assert call_kwargs.kwargs["json"]["companyName"] == "Acme"
    assert call_kwargs.kwargs["json"]["systems"][0]["publicFacing"] is True


@pytest.mark.asyncio
async def test_score_architecture_risk_reports_connection_errors():
    import httpx

    params = ScoreArchitectureRiskInput(
        company_name="Acme",
        systems=[SystemInput(name="x", type="API", criticality="LOW")],
    )

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.ConnectError("boom"))):
        result = await cloudsealed_score_architecture_risk(params)

    assert result.startswith("Error: Could not reach Predictive-ML-Core")


# billing where EC2 dominates spend and spikes; RDS steady; S3 tiny
_MULTI_SERVICE_CSV = "date,service,cost\n" + "".join(
    f"2026-01-{d:02d},EC2,{500 + (3000 if d == 20 else 0)}\n"
    f"2026-01-{d:02d},RDS,200\n"
    f"2026-01-{d:02d},S3,50\n"
    for d in range(1, 29)
)

_RISK_REPORT = {
    "predictions": [
        {"systemName": "EC2", "riskScores": {"singlePointOfFailure": 60, "excessiveCoupling": 40, "scalabilityGap": 15}, "findings": [], "recommendations": []},
        {"systemName": "RDS", "riskScores": {"singlePointOfFailure": 70, "excessiveCoupling": 5, "scalabilityGap": 35}, "findings": [], "recommendations": []},
        {"systemName": "S3", "riskScores": {"singlePointOfFailure": 5, "excessiveCoupling": 5, "scalabilityGap": 0}, "findings": [], "recommendations": []},
    ],
    "architectureSummary": "ok",
    "overallArchitectureScore": 69,
}


def _correlate_params(response_format="json", systems=None):
    return CorrelateCostAndRiskInput(
        company_name="Acme",
        csv_content=_MULTI_SERVICE_CSV,
        systems=systems or [
            SystemInput(name="EC2", type="APPLICATION", criticality="CRITICAL", public_facing=True),
            SystemInput(name="RDS", type="DATABASE", criticality="CRITICAL", auth_method="MTLS"),
            SystemInput(name="S3", type="APPLICATION", criticality="LOW"),
        ],
        response_format=response_format,
    )


def _mock_risk_response():
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = _RISK_REPORT
    return mock


@pytest.mark.asyncio
async def test_correlate_ranks_costly_and_risky_system_first():
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_mock_risk_response())):
        result = await cloudsealed_correlate_cost_and_risk(_correlate_params("json"))
    parsed = json.loads(result)

    # EC2 is both the biggest spender and a public-facing critical system —
    # it must rank first by double-jeopardy.
    assert parsed["correlations"][0]["systemName"] == "EC2"
    assert parsed["correlations"][0]["matchedBillingService"] == "EC2"
    assert parsed["correlations"][0]["doubleJeopardyScore"] >= parsed["correlations"][1]["doubleJeopardyScore"]
    # S3 (cheap + low risk) must rank last.
    assert parsed["correlations"][-1]["systemName"] == "S3"


@pytest.mark.asyncio
async def test_correlate_flags_unmatched_systems():
    # A system whose name matches no billing service (EC2/RDS/S3) must surface
    # matchedBillingService=null and trigger the unmatched note.
    systems = [SystemInput(name="checkout-api", type="API", criticality="CRITICAL", public_facing=True)]
    unmatched_report = MagicMock()
    unmatched_report.raise_for_status = MagicMock()
    unmatched_report.json.return_value = {
        "predictions": [
            {"systemName": "checkout-api", "riskScores": {"singlePointOfFailure": 60, "excessiveCoupling": 40, "scalabilityGap": 15}, "findings": [], "recommendations": []},
        ],
        "architectureSummary": "ok",
        "overallArchitectureScore": 58,
    }
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=unmatched_report)):
        result = await cloudsealed_correlate_cost_and_risk(_correlate_params("json", systems))
    parsed = json.loads(result)

    assert parsed["correlations"][0]["matchedBillingService"] is None
    assert parsed["unmatchedNote"]


@pytest.mark.asyncio
async def test_correlate_reports_csv_parse_errors():
    params = CorrelateCostAndRiskInput(
        company_name="Acme",
        csv_content="foo,bar\nbaz,qux",
        systems=[SystemInput(name="x", type="API", criticality="LOW")],
    )
    # No HTTP call should be needed — CSV parse fails first.
    result = await cloudsealed_correlate_cost_and_risk(params)
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_correlate_markdown_has_double_jeopardy_table():
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_mock_risk_response())):
        result = await cloudsealed_correlate_cost_and_risk(_correlate_params("markdown"))
    assert "Double jeopardy" in result
    assert "| EC2 |" in result
