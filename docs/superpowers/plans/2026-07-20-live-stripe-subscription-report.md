# Live Stripe Subscription Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only native nanobot tool that retrieves every SaleShow subscription directly from Stripe on each scheduled run and never presents cached data as live.

**Architecture:** A plugin-discovered `StripeSubscriptionReportTool` owns Stripe HTTP access, pagination, normalization, and sanitized failure reporting. `ToolsConfig` exposes its configuration and Coolify renders the existing secret into that configuration while removing Stripe's broken `mcp-remote` entry. The existing cron job continues to trigger the agent; the tool description and job prompt require a fresh tool result for every report.

**Tech Stack:** Python 3.11+, `httpx`, Pydantic 2, pytest, pytest-asyncio, Docker Compose.

## Global Constraints

- Base implementation on `origin/main` (`569c9d4` at design time), not the stale local ancestor.
- The tool is read-only and must never create, update, or delete Stripe resources.
- Retrieve `/v1/subscriptions` with `status=all`, `limit=100`, `expand[]=data.customer`, following every `has_more` page.
- Never log or return the Stripe API key or untrusted Stripe response bodies.
- A failed page invalidates the complete snapshot; never return partial counts as complete.
- Retry only transport failures, `429`, and `5xx`; do not retry permanent `4xx` responses.
- Do not use remembered or cached subscription data when a live call fails.

---

### Task 1: Add Stripe Tool Configuration and Plugin Registration

**Files:**
- Create: `nanobot/agent/tools/stripe.py`
- Modify: `nanobot/config/schema.py`
- Create: `tests/tools/test_stripe_subscription_report.py`

**Interfaces:**
- Produces: `StripeSubscriptionReportConfig(enable: bool, api_key: str, timeout: float, max_retries: int)`.
- Produces: `StripeSubscriptionReportTool(config, transport=None, sleep=asyncio.sleep)` registered as `stripe_subscription_report`.
- Consumes: `ToolContext.config.stripe` through the existing `ToolLoader` plugin convention.

- [ ] **Step 1: Write failing configuration and registration tests**

```python
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolsConfig


def test_stripe_config_accepts_camel_case_api_key():
    config = ToolsConfig.model_validate({
        "stripe": {"enable": True, "apiKey": "sk_test_example", "timeout": 12, "maxRetries": 3}
    })
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
```

- [ ] **Step 2: Run tests and verify the missing configuration fails**

Run: `pytest -q tests/tools/test_stripe_subscription_report.py`

Expected: FAIL because `ToolsConfig` has no `stripe` field and the tool module does not exist.

- [ ] **Step 3: Add the minimal configuration and discoverable tool shell**

Create `nanobot/agent/tools/stripe.py` with:

```python
"""Read-only live Stripe subscription reporting tool."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.config.schema import Base


class StripeSubscriptionReportConfig(Base):
    enable: bool = True
    api_key: str = ""
    timeout: float = 20.0
    max_retries: int = 2


@tool_parameters({"type": "object", "properties": {}, "additionalProperties": False})
class StripeSubscriptionReportTool(Tool):
    name = "stripe_subscription_report"
    description = (
        "Retrieve a complete live snapshot of Stripe subscriptions. "
        "Use this for every current SaleShow subscription report; never replace a failed "
        "live call with cached or remembered subscription data."
    )
    config_key = "stripe"

    @classmethod
    def config_cls(cls):
        return StripeSubscriptionReportConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return bool(ctx.config.stripe.enable and ctx.config.stripe.api_key.strip())

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(ctx.config.stripe)

    def __init__(
        self,
        config: StripeSubscriptionReportConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self._transport = transport
        self._sleep = sleep

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        raise NotImplementedError
```

Modify `ToolsConfig` in `nanobot/config/schema.py`:

```python
stripe: StripeSubscriptionReportConfig = Field(
    default_factory=lambda: _lazy_default(
        "nanobot.agent.tools.stripe", "StripeSubscriptionReportConfig"
    )
)
```

Import and re-export `StripeSubscriptionReportConfig` inside `_resolve_tool_config_refs()` before calling `model_rebuild()`.

- [ ] **Step 4: Run registration tests**

Run: `pytest -q tests/tools/test_stripe_subscription_report.py tests/tools/test_tool_loader.py`

Expected: PASS.

- [ ] **Step 5: Commit the configuration and registration slice**

```bash
git add nanobot/agent/tools/stripe.py nanobot/config/schema.py tests/tools/test_stripe_subscription_report.py
git commit -m "feat: register live Stripe report tool"
```

---

### Task 2: Implement Complete Pagination and Subscription Normalization

**Files:**
- Modify: `nanobot/agent/tools/stripe.py`
- Modify: `tests/tools/test_stripe_subscription_report.py`

**Interfaces:**
- Consumes: Stripe list payloads shaped as `{object, data, has_more}`.
- Produces: JSON with `ok`, `source`, `retrieved_at`, `total`, `by_status`, `by_interval`, `subscriptions`, and `pages`.
- Produces: `_billing_interval(subscription) -> str` returning `month`, `year`, `mixed`, or `unknown`.

- [ ] **Step 1: Add a reusable mocked Stripe transport and failing pagination test**

```python
import json

import httpx
import pytest

from nanobot.agent.tools.stripe import (
    StripeSubscriptionReportConfig,
    StripeSubscriptionReportTool,
)


def _subscription(sub_id, status, interval, customer):
    return {
        "id": sub_id,
        "status": status,
        "customer": customer,
        "cancel_at_period_end": False,
        "current_period_start": 1_700_000_000,
        "current_period_end": 1_702_592_000,
        "items": {"data": [{"price": {
            "id": f"price_{interval}",
            "product": "prod_saleshow",
            "recurring": {"interval": interval},
        }}]},
    }


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
            return httpx.Response(200, json={
                "object": "list",
                "data": [_subscription(
                    "sub_month", "active", "month",
                    {"id": "cus_1", "name": "PINSA", "email": "pinsa@example.com"},
                )],
                "has_more": True,
            })
        assert request.url.params["starting_after"] == "sub_month"
        return httpx.Response(200, json={
            "object": "list",
            "data": [_subscription("sub_year", "past_due", "year", "cus_2")],
            "has_more": False,
        })

    tool = StripeSubscriptionReportTool(
        StripeSubscriptionReportConfig(api_key="sk_test_example"),
        transport=httpx.MockTransport(handler),
    )
    result = json.loads(await tool.execute())

    assert result["ok"] is True
    assert result["source"] == "stripe_api_live"
    assert result["total"] == 2
    assert result["pages"] == 2
    assert result["by_status"] == {"active": 1, "past_due": 1}
    assert result["by_interval"] == {"month": 1, "year": 1}
    assert result["subscriptions"][0]["customer_name"] == "PINSA"
    assert result["subscriptions"][1]["customer_id"] == "cus_2"
    assert len(requests) == 2
```

- [ ] **Step 2: Run the pagination test and verify it fails at `NotImplementedError`**

Run: `pytest -q tests/tools/test_stripe_subscription_report.py::test_report_fetches_all_pages_and_normalizes_subscriptions`

Expected: FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement live retrieval and normalization**

Implement these focused helpers in `stripe.py`:

```python
_STRIPE_URL = "https://api.stripe.com/v1/subscriptions"


def _customer_fields(value: Any) -> tuple[str | None, str | None, str | None]:
    if isinstance(value, dict):
        return value.get("id"), value.get("name"), value.get("email")
    return (value if isinstance(value, str) else None), None, None


def _billing_interval(subscription: dict[str, Any]) -> str:
    items = subscription.get("items", {}).get("data", [])
    intervals = {
        item.get("price", {}).get("recurring", {}).get("interval")
        for item in items
    }
    intervals.discard(None)
    if not intervals:
        return "unknown"
    if len(intervals) > 1:
        return "mixed"
    interval = next(iter(intervals))
    return interval if interval in {"month", "year"} else "unknown"
```

Add `_normalize_subscription()` to produce only the fields declared in the design, and implement `execute()` so it:

1. Creates one `httpx.AsyncClient` with Bearer authorization, timeout, and injected test transport.
2. Requests pages with `status`, `limit`, and `expand[]` query parameters.
3. Sets `starting_after` to the final subscription ID when `has_more` is true.
4. Builds counters only after every page succeeds.
5. Serializes with `json.dumps(..., ensure_ascii=False)` and a UTC ISO timestamp.

- [ ] **Step 4: Add failing interval edge-case tests, then implement the smallest normalization change**

```python
@pytest.mark.parametrize(
    ("intervals", "expected"),
    [(["month"], "month"), (["year"], "year"),
     (["month", "year"], "mixed"), ([], "unknown"), (["week"], "unknown")],
)
def test_billing_interval_classification(intervals, expected):
    subscription = {"items": {"data": [
        {"price": {"recurring": {"interval": interval}}} for interval in intervals
    ]}}
    assert _billing_interval(subscription) == expected
```

Run before implementation: `pytest -q tests/tools/test_stripe_subscription_report.py::test_billing_interval_classification`

Expected: at least one case FAIL until all classifications are implemented.

- [ ] **Step 5: Run the complete Stripe tool test file**

Run: `pytest -q tests/tools/test_stripe_subscription_report.py`

Expected: PASS.

- [ ] **Step 6: Commit pagination and normalization**

```bash
git add nanobot/agent/tools/stripe.py tests/tools/test_stripe_subscription_report.py
git commit -m "feat: fetch complete Stripe subscription snapshots"
```

---

### Task 3: Add Bounded Retries and Sanitized Failures

**Files:**
- Modify: `nanobot/agent/tools/stripe.py`
- Modify: `tests/tools/test_stripe_subscription_report.py`

**Interfaces:**
- Produces: failure JSON `{ok: false, source: "stripe_api_live", error: {category, status_code, request_id}}`.
- Produces: `_request_page(client, params)` with bounded retry behavior.

- [ ] **Step 1: Write failing retry and permanent-error tests**

```python
@pytest.mark.asyncio
async def test_transient_failure_retries_then_succeeds():
    calls = 0
    sleeps = []

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, headers={"Request-Id": "req_retry"}, json={"error": {}})
        return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

    async def fake_sleep(delay):
        sleeps.append(delay)

    tool = StripeSubscriptionReportTool(
        StripeSubscriptionReportConfig(api_key="sk_test_example", max_retries=2),
        transport=httpx.MockTransport(handler), sleep=fake_sleep,
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
        "error": {"category": "authentication", "status_code": 401, "request_id": "req_auth"},
    }
    assert "sk_test_example" not in raw
    assert "secret response text" not in raw
```

- [ ] **Step 2: Write a failing later-page test proving partial data is discarded**

```python
@pytest.mark.asyncio
async def test_later_page_failure_returns_no_partial_snapshot():
    def handler(request):
        if "starting_after" not in request.url.params:
            return httpx.Response(200, json={
                "object": "list",
                "data": [_subscription("sub_first", "active", "month", "cus_1")],
                "has_more": True,
            })
        return httpx.Response(403, headers={"Request-Id": "req_page_2"}, json={"error": {}})

    tool = StripeSubscriptionReportTool(
        StripeSubscriptionReportConfig(api_key="sk_test_example"),
        transport=httpx.MockTransport(handler),
    )
    result = json.loads(await tool.execute())
    assert result["ok"] is False
    assert "subscriptions" not in result
    assert "total" not in result
```

- [ ] **Step 3: Run the error tests and verify expected failures**

Run: `pytest -q tests/tools/test_stripe_subscription_report.py -k 'transient or auth or later_page'`

Expected: FAIL because retry classification and sanitized failure serialization are not implemented.

- [ ] **Step 4: Implement retry classification and sanitized error objects**

Add an internal `StripeReportError` carrying only `category`, `status_code`, and `request_id`. `_request_page()` must:

- retry `httpx.TransportError`, `429`, and `500..599` through `max_retries`;
- use `Retry-After` when it is a non-negative number, otherwise `2 ** attempt` seconds;
- classify `401/403` as `authentication`, other permanent `4xx` as `request`, exhausted transient errors as `unavailable`, and malformed success payloads as `invalid_response`;
- never copy Stripe response bodies into exceptions, logs, or tool output.

Catch `StripeReportError` once at the outer `execute()` boundary and return the exact failure contract from Step 1.

- [ ] **Step 5: Add and pass a `429 Retry-After` test**

```python
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
        transport=httpx.MockTransport(handler), sleep=fake_sleep,
    )
    assert json.loads(await tool.execute())["ok"] is True
    assert sleeps == [2.5]
```

Run: `pytest -q tests/tools/test_stripe_subscription_report.py`

Expected: PASS.

- [ ] **Step 6: Commit retry and failure behavior**

```bash
git add nanobot/agent/tools/stripe.py tests/tools/test_stripe_subscription_report.py
git commit -m "feat: harden Stripe report failures and retries"
```

---

### Task 4: Wire the Native Tool into the Coolify Configuration

**Files:**
- Modify: `docker-compose.yml`
- Create: `tests/test_coolify_stripe_config.py`

**Interfaces:**
- Consumes: Coolify's existing `STRIPE_SECRET_KEY` variable.
- Produces: `tools.stripe.apiKey` in `/home/nanobot/.nanobot/config.json`.
- Removes: the `tools.mcpServers.stripe` `mcp-remote@latest` configuration.

- [ ] **Step 1: Write a failing Compose contract test**

```python
from pathlib import Path


def test_coolify_uses_native_stripe_report_config():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert '"stripe": {' in compose
    assert '"apiKey": "$${STRIPE_SECRET_KEY}"' in compose
    assert '"timeout": 20' in compose
    assert '"maxRetries": 2' in compose
    assert '"STRIPE_AUTH_HEADER"' not in compose
    assert '"https://mcp.stripe.com/"' not in compose
```

- [ ] **Step 2: Run the contract test and verify it fails against the MCP configuration**

Run: `pytest -q tests/test_coolify_stripe_config.py`

Expected: FAIL because Compose still contains `STRIPE_AUTH_HEADER` and `mcp.stripe.com`.

- [ ] **Step 3: Replace only the Stripe MCP block**

Within the generated `tools` object, add:

```json
"stripe": {
  "enable": true,
  "apiKey": "$${STRIPE_SECRET_KEY}",
  "timeout": 20,
  "maxRetries": 2
},
```

Remove only the `mcpServers.stripe` entry. Preserve `fencode_blog_mcp` and `minimax` unchanged.

- [ ] **Step 4: Validate Compose rendering without exposing secrets**

Run with non-secret placeholders:

```bash
PROVIDER_API_KEY=x LLM_MODEL=x TELEGRAM_ALLOWED_USERS='[]' DISCORD_ALLOWED_USERS='[]' \
BRAVE_API_KEY=x STRIPE_SECRET_KEY=x docker compose config --quiet
```

Expected: exit code 0. Warnings about the obsolete top-level `version` key are acceptable; interpolation or YAML errors are not.

- [ ] **Step 5: Run configuration and loader tests**

Run: `pytest -q tests/test_coolify_stripe_config.py tests/tools/test_tool_loader.py tests/config/test_env_interpolation.py`

Expected: PASS.

- [ ] **Step 6: Commit Coolify wiring**

```bash
git add docker-compose.yml tests/test_coolify_stripe_config.py
git commit -m "fix: use native Stripe reporting in Coolify"
```

---

### Task 5: Verify the Agent Contract and Full Regression Suite

**Files:**
- Modify: `tests/tools/test_stripe_subscription_report.py`
- Operational after deployment: update existing cron job payload in nanobot's persistent store through the supported cron interface.

**Interfaces:**
- Ensures: the tool description tells the model to use a live result and forbids stale fallback.
- Requires operational prompt: `Generar Status Semanal SaleSho` explicitly calls `stripe_subscription_report`.

- [ ] **Step 1: Add a failing description contract test**

```python
def test_tool_description_requires_live_data_and_forbids_stale_fallback():
    description = StripeSubscriptionReportTool.description.lower()
    assert "live" in description
    assert "every" in description
    assert "never" in description
    assert "cached or remembered" in description
```

- [ ] **Step 2: Run the description test and tighten the description only if it fails**

Run: `pytest -q tests/tools/test_stripe_subscription_report.py::test_tool_description_requires_live_data_and_forbids_stale_fallback`

Expected: PASS if Task 1 used the exact description; otherwise FAIL and then pass after the minimal wording correction.

- [ ] **Step 3: Run focused formatting, lint, and tests**

```bash
ruff check nanobot/agent/tools/stripe.py nanobot/config/schema.py \
  tests/tools/test_stripe_subscription_report.py tests/test_coolify_stripe_config.py
pytest -q tests/tools/test_stripe_subscription_report.py \
  tests/test_coolify_stripe_config.py tests/tools/test_tool_loader.py \
  tests/config/test_env_interpolation.py
```

Expected: zero lint errors and all selected tests pass.

- [ ] **Step 4: Run the complete repository test suite**

Run: `pytest -q`

Expected: all tests pass. If unrelated pre-existing failures exist, record their exact test names and verify that the focused Stripe suite remains green.

- [ ] **Step 5: Review the final diff for credential exposure and scope**

```bash
git diff origin/main...HEAD --check
git diff origin/main...HEAD -- . ':!docs/superpowers'
rg -n 'sk_(live|test|restricted)_[A-Za-z0-9]+' nanobot tests docker-compose.yml
```

Expected: no whitespace errors, no literal Stripe keys, and changes limited to the native tool, configuration, Compose wiring, and tests.

- [ ] **Step 6: Commit any final test-contract adjustment**

```bash
git add tests/tools/test_stripe_subscription_report.py nanobot/agent/tools/stripe.py
git commit -m "test: verify live Stripe reporting contract"
```

- [ ] **Step 7: Production handoff after code deployment**

Update the existing `Generar Status Semanal SaleSho` job message to say:

```text
Call stripe_subscription_report during this execution. Generate the SaleShow report only
from a successful result whose source is stripe_api_live. Include retrieved_at in the report.
If the tool fails, report that live Stripe data is unavailable. Never use cached, remembered,
or previous report values as a fallback.
```

Then invoke the job once manually and confirm the delivered report contains the new execution timestamp. This operational step is deliberately not performed by repository tests and requires the deployed revision.
