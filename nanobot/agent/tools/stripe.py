"""Read-only live Stripe subscription reporting tool."""

from __future__ import annotations

import asyncio
import json
import math
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import Field

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.config.schema import Base

_STRIPE_URL = "https://api.stripe.com/v1/subscriptions"
_MAX_RETRY_DELAY = 30.0
_KNOWN_STATUSES = (
    "active",
    "trialing",
    "past_due",
    "unpaid",
    "paused",
    "canceled",
    "incomplete",
    "incomplete_expired",
)


class StripeReportError(Exception):
    """Sanitized failure details safe to return to the agent."""

    def __init__(
        self,
        category: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.status_code = status_code
        self.request_id = request_id


def _resource_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("id"), str):
        return value["id"]
    return None


def _customer_fields(value: Any) -> tuple[str | None, str | None, str | None]:
    if isinstance(value, dict):
        return _resource_id(value), value.get("name"), value.get("email")
    return _resource_id(value), None, None


def _billing_interval(subscription: dict[str, Any]) -> str:
    items = subscription.get("items", {}).get("data", [])
    intervals = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("price"), dict):
            continue
        recurring = item["price"].get("recurring") or {}
        intervals.add(recurring.get("interval"))
    intervals.discard(None)
    if not intervals:
        return "unknown"
    if len(intervals) > 1:
        return "mixed"
    interval = next(iter(intervals))
    return interval if interval in {"month", "year"} else "unknown"


def _normalize_subscription(subscription: dict[str, Any]) -> dict[str, Any]:
    customer_id, customer_name, customer_email = _customer_fields(subscription.get("customer"))
    items = []
    for item in subscription.get("items", {}).get("data", []):
        price = item.get("price", {}) if isinstance(item, dict) else {}
        recurring = (price.get("recurring") or {}) if isinstance(price, dict) else {}
        items.append(
            {
                "price_id": _resource_id(price),
                "product_id": _resource_id(price.get("product")),
                "interval": recurring.get("interval"),
            }
        )
    return {
        "id": subscription.get("id"),
        "status": subscription.get("status"),
        "customer_id": customer_id,
        "customer_name": customer_name,
        "customer_email": customer_email,
        "cancel_at_period_end": bool(subscription.get("cancel_at_period_end", False)),
        "current_period_start": subscription.get("current_period_start"),
        "current_period_end": subscription.get("current_period_end"),
        "billing_interval": _billing_interval(subscription),
        "items": items,
    }


def _validate_subscription(subscription: dict[str, Any]) -> None:
    customer = subscription.get("customer")
    if customer is not None and not isinstance(customer, (str, dict)):
        raise StripeReportError("invalid_response", status_code=200)

    items = subscription.get("items", {})
    if not isinstance(items, dict):
        raise StripeReportError("invalid_response", status_code=200)
    item_data = items.get("data", [])
    if not isinstance(item_data, list):
        raise StripeReportError("invalid_response", status_code=200)
    for item in item_data:
        if not isinstance(item, dict):
            raise StripeReportError("invalid_response", status_code=200)
        price = item.get("price", {})
        if not isinstance(price, dict):
            raise StripeReportError("invalid_response", status_code=200)
        recurring = price.get("recurring")
        if recurring is not None and not isinstance(recurring, dict):
            raise StripeReportError("invalid_response", status_code=200)


class StripeSubscriptionReportConfig(Base):
    """Configuration for live Stripe subscription snapshots."""

    enable: bool = True
    api_key: str = ""
    timeout: float = Field(default=20.0, gt=0, le=120)
    max_retries: int = Field(default=2, ge=0, le=5)


@tool_parameters({"type": "object", "properties": {}, "additionalProperties": False})
class StripeSubscriptionReportTool(Tool):
    """Retrieve the complete current subscription state from Stripe."""

    name = "stripe_subscription_report"
    description = (
        "Retrieve a complete live snapshot of Stripe subscriptions. "
        "Use this for every current SaleShow subscription report; never replace a failed "
        "live call with cached or remembered subscription data."
    )
    config_key = "stripe"

    @classmethod
    def config_cls(cls) -> type[StripeSubscriptionReportConfig]:
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

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            try:
                retry_after = float(response.headers.get("Retry-After", ""))
                if math.isfinite(retry_after) and retry_after >= 0:
                    return min(retry_after, _MAX_RETRY_DELAY)
            except ValueError:
                pass
        return float(2**attempt)

    async def _request_page(
        self, client: httpx.AsyncClient, params: dict[str, Any]
    ) -> dict[str, Any]:
        for attempt in range(self.config.max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = await client.get(_STRIPE_URL, params=params)
            except httpx.TransportError:
                if attempt < self.config.max_retries:
                    await self._sleep(self._retry_delay(None, attempt))
                    continue
                raise StripeReportError("unavailable") from None

            status_code = response.status_code
            request_id = response.headers.get("Request-Id")
            transient = status_code == 429 or 500 <= status_code <= 599
            if transient:
                if attempt < self.config.max_retries:
                    await self._sleep(self._retry_delay(response, attempt))
                    continue
                raise StripeReportError(
                    "unavailable", status_code=status_code, request_id=request_id
                )
            if status_code in {401, 403}:
                raise StripeReportError(
                    "authentication", status_code=status_code, request_id=request_id
                )
            if not 200 <= status_code <= 299:
                raise StripeReportError(
                    "request", status_code=status_code, request_id=request_id
                )

            try:
                payload = response.json()
            except ValueError:
                raise StripeReportError(
                    "invalid_response", status_code=status_code, request_id=request_id
                ) from None
            if (
                not isinstance(payload, dict)
                or payload.get("object") != "list"
                or not isinstance(payload.get("data"), list)
                or not isinstance(payload.get("has_more"), bool)
            ):
                raise StripeReportError(
                    "invalid_response", status_code=status_code, request_id=request_id
                )
            return payload

        raise StripeReportError("unavailable")  # pragma: no cover

    @staticmethod
    def _failure(error: StripeReportError) -> str:
        return json.dumps(
            {
                "ok": False,
                "source": "stripe_api_live",
                "error": {
                    "category": error.category,
                    "status_code": error.status_code,
                    "request_id": error.request_id,
                },
            },
            ensure_ascii=False,
        )

    async def execute(self, **kwargs: Any) -> str:
        subscriptions: list[dict[str, Any]] = []
        pages = 0
        params: dict[str, Any] = {
            "status": "all",
            "limit": 100,
            "expand[]": "data.customer",
        }
        try:
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                timeout=self.config.timeout,
                transport=self._transport,
            ) as client:
                while True:
                    payload = await self._request_page(client, params)
                    page = payload["data"]
                    if not all(
                        isinstance(subscription, dict)
                        and isinstance(subscription.get("id"), str)
                        for subscription in page
                    ):
                        raise StripeReportError("invalid_response", status_code=200)
                    for subscription in page:
                        _validate_subscription(subscription)
                    subscriptions.extend(page)
                    pages += 1
                    if not payload["has_more"]:
                        break
                    if not page:
                        raise StripeReportError("invalid_response", status_code=200)
                    params["starting_after"] = page[-1]["id"]
        except StripeReportError as error:
            return self._failure(error)

        normalized = [_normalize_subscription(subscription) for subscription in subscriptions]
        observed_statuses = Counter(item.get("status") or "unknown" for item in normalized)
        by_status = {status: observed_statuses.pop(status, 0) for status in _KNOWN_STATUSES}
        by_status.update(observed_statuses)
        by_interval = Counter(item["billing_interval"] for item in normalized)
        return json.dumps(
            {
                "ok": True,
                "source": "stripe_api_live",
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "total": len(normalized),
                "by_status": dict(by_status),
                "by_interval": dict(by_interval),
                "subscriptions": normalized,
                "pages": pages,
            },
            ensure_ascii=False,
        )
