"""Tests for the live Stripe subscription reporting tool."""

import json

import httpx
import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.stripe import (
    StripeSubscriptionReportConfig,
    StripeSubscriptionReportTool,
    _billing_interval,
)
from nanobot.config.schema import ToolsConfig


def _subscription(sub_id, status, interval, customer):
    return {
        "id": sub_id,
        "status": status,
        "customer": customer,
        "cancel_at_period_end": False,
        "current_period_start": 1_700_000_000,
        "current_period_end": 1_702_592_000,
        "items": {
            "data": [
                {
                    "price": {
                        "id": f"price_{interval}",
                        "product": "prod_saleshow",
                        "recurring": {"interval": interval},
                    }
                }
            ]
        },
    }


def test_stripe_config_accepts_camel_case_api_key():
    config = ToolsConfig.model_validate(
        {
            "stripe": {
                "enable": True,
                "apiKey": "sk_test_example",
                "timeout": 12,
                "maxRetries": 3,
            }
        }
    )

    assert config.stripe.api_key == "sk_test_example"
    assert config.stripe.timeout == 12
    assert config.stripe.max_retries == 3


def test_loader_registers_stripe_report_only_when_key_is_present(tmp_path):
    enabled = ToolsConfig.model_validate({"stripe": {"apiKey": "sk_test_example"}})
    registry = ToolRegistry()
    ToolLoader().load(ToolContext(config=enabled, workspace=str(tmp_path)), registry)
    assert registry.has("stripe_subscription_report")

    disabled = ToolsConfig()
    empty_registry = ToolRegistry()
    ToolLoader().load(ToolContext(config=disabled, workspace=str(tmp_path)), empty_registry)
    assert not empty_registry.has("stripe_subscription_report")


def test_tool_description_requires_live_data_and_forbids_stale_fallback():
    description = StripeSubscriptionReportTool.description.lower()
    assert "live" in description
    assert "every" in description
    assert "never" in description
    assert "cached or remembered" in description


@pytest.mark.asyncio
async def test_report_fetches_all_pages_and_normalizes_subscriptions():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer sk_test_example"
        assert request.url.params["status"] == "all"
        assert request.url.params["limit"] == "100"
        assert request.url.params["expand[]"] == "data.customer"
        if "starting_after" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        _subscription(
                            "sub_month",
                            "active",
                            "month",
                            {
                                "id": "cus_1",
                                "name": "PINSA",
                                "email": "pinsa@example.com",
                            },
                        )
                    ],
                    "has_more": True,
                },
            )
        assert request.url.params["starting_after"] == "sub_month"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [_subscription("sub_year", "past_due", "year", "cus_2")],
                "has_more": False,
            },
        )

    tool = StripeSubscriptionReportTool(
        StripeSubscriptionReportConfig(api_key="sk_test_example"),
        transport=httpx.MockTransport(handler),
    )
    result = json.loads(await tool.execute())

    assert result["ok"] is True
    assert result["source"] == "stripe_api_live"
    assert result["total"] == 2
    assert result["pages"] == 2
    assert result["by_status"] == {
        "active": 1,
        "trialing": 0,
        "past_due": 1,
        "unpaid": 0,
        "paused": 0,
        "canceled": 0,
        "incomplete": 0,
        "incomplete_expired": 0,
    }
    assert result["by_interval"] == {"month": 1, "year": 1}
    assert result["subscriptions"][0]["customer_name"] == "PINSA"
    assert result["subscriptions"][1]["customer_id"] == "cus_2"
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("intervals", "expected"),
    [
        (["month"], "month"),
        (["year"], "year"),
        (["month", "year"], "mixed"),
        ([], "unknown"),
        (["week"], "unknown"),
    ],
)
def test_billing_interval_classification(intervals, expected):
    subscription = {
        "items": {
            "data": [
                {"price": {"recurring": {"interval": interval}}} for interval in intervals
            ]
        }
    }
    assert _billing_interval(subscription) == expected


@pytest.mark.asyncio
async def test_transient_failure_retries_then_succeeds():
    calls = 0
    sleeps = []

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                500, headers={"Request-Id": "req_retry"}, json={"error": {}}
            )
        return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

    async def fake_sleep(delay):
        sleeps.append(delay)

    tool = StripeSubscriptionReportTool(
        StripeSubscriptionReportConfig(api_key="sk_test_example", max_retries=2),
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
    )
    result = json.loads(await tool.execute())
    assert result["ok"] is True
    assert calls == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_auth_failure_is_sanitized_and_not_retried():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            401,
            headers={"Request-Id": "req_auth"},
            json={"error": {"message": "secret response text must not escape"}},
        )

    tool = StripeSubscriptionReportTool(
        StripeSubscriptionReportConfig(api_key="sk_test_example", max_retries=2),
        transport=httpx.MockTransport(handler),
    )
    raw = await tool.execute()
    result = json.loads(raw)
    assert calls == 1
    assert result == {
        "ok": False,
        "source": "stripe_api_live",
        "error": {
            "category": "authentication",
            "status_code": 401,
            "request_id": "req_auth",
        },
    }
    assert "sk_test_example" not in raw
    assert "secret response text" not in raw


@pytest.mark.asyncio
async def test_later_page_failure_returns_no_partial_snapshot():
    def handler(request):
        if "starting_after" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [_subscription("sub_first", "active", "month", "cus_1")],
                    "has_more": True,
                },
            )
        return httpx.Response(
            403,
            headers={"Request-Id": "req_page_2"},
            json={"error": {}},
        )

    tool = StripeSubscriptionReportTool(
        StripeSubscriptionReportConfig(api_key="sk_test_example"),
        transport=httpx.MockTransport(handler),
    )
    result = json.loads(await tool.execute())
    assert result["ok"] is False
    assert "subscriptions" not in result
    assert "total" not in result


@pytest.mark.asyncio
async def test_rate_limit_honors_retry_after():
    calls = 0
    sleeps = []

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2.5"}, json={"error": {}})
        return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

    async def fake_sleep(delay):
        sleeps.append(delay)

    tool = StripeSubscriptionReportTool(
        StripeSubscriptionReportConfig(api_key="sk_test_example", max_retries=1),
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
    )
    assert json.loads(await tool.execute())["ok"] is True
    assert sleeps == [2.5]


@pytest.mark.asyncio
async def test_malformed_success_is_sanitized():
    tool = StripeSubscriptionReportTool(
        StripeSubscriptionReportConfig(api_key="sk_test_example"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"object": "list", "has_more": False})
        ),
    )

    assert json.loads(await tool.execute()) == {
        "ok": False,
        "source": "stripe_api_live",
        "error": {
            "category": "invalid_response",
            "status_code": 200,
            "request_id": None,
        },
    }


@pytest.mark.asyncio
async def test_status_counts_include_zeros_and_preserve_unknown_statuses():
    statuses = [
        "active",
        "trialing",
        "past_due",
        "unpaid",
        "paused",
        "canceled",
        "incomplete",
        "incomplete_expired",
        "future_status",
    ]
    payload = {
        "object": "list",
        "data": [
            _subscription(f"sub_{index}", status, "month", f"cus_{index}")
            for index, status in enumerate(statuses)
        ],
        "has_more": False,
    }
    tool = StripeSubscriptionReportTool(
        StripeSubscriptionReportConfig(api_key="sk_test_example"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )

    result = json.loads(await tool.execute())

    assert result["by_status"] == {status: 1 for status in statuses}


@pytest.mark.asyncio
async def test_malformed_nested_subscription_is_sanitized():
    payload = {
        "object": "list",
        "data": [{"id": "sub_bad", "status": "active", "items": None}],
        "has_more": False,
    }
    tool = StripeSubscriptionReportTool(
        StripeSubscriptionReportConfig(api_key="sk_test_example"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )

    assert json.loads(await tool.execute()) == {
        "ok": False,
        "source": "stripe_api_live",
        "error": {
            "category": "invalid_response",
            "status_code": 200,
            "request_id": None,
        },
    }


@pytest.mark.asyncio
async def test_non_finite_retry_after_uses_bounded_backoff():
    calls = 0
    sleeps = []

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "inf"}, json={"error": {}})
        return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

    async def fake_sleep(delay):
        sleeps.append(delay)

    tool = StripeSubscriptionReportTool(
        StripeSubscriptionReportConfig(api_key="sk_test_example", max_retries=1),
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
    )

    assert json.loads(await tool.execute())["ok"] is True
    assert sleeps == [1.0]
