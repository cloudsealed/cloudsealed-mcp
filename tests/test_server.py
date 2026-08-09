import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cloudsealed_mcp.server import (
    AnalyzeBillingWasteInput,
    ScoreArchitectureRiskInput,
    SystemInput,
    cloudsealed_analyze_billing_waste,
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
