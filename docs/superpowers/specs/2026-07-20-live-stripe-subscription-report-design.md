# Live Stripe Subscription Report Design

## Goal

When the existing weekly SaleShow job runs, nanobot must retrieve the current subscription state directly from Stripe and generate the report from that response. It must never label cached or remembered data as current.

## Scope

This change adds one read-only Stripe reporting tool and wires it into the existing agent tool registry. It does not add a webhook receiver, local subscription database, payment mutation capability, or changes to the existing job schedule and delivery channel.

The production deployment currently runs commit `569c9d4`, while this checkout is at `34fdaf59`. Implementation must first reconcile the checkout with the production branch so the change is built on the code Coolify actually deploys.

## Architecture

Add a dedicated native tool named `stripe_subscription_report`. The tool calls Stripe's REST API directly over HTTPS using the configured secret or restricted key. It does not use `mcp-remote`, Stripe OAuth, or an LLM-generated sequence of generic Stripe calls.

The existing scheduled job remains responsible for triggering the agent and delivering its response. On every run, the agent calls `stripe_subscription_report`, receives a structured snapshot, and formats the human-readable SaleShow report.

```text
weekly cron job
  -> agent turn
  -> stripe_subscription_report
  -> Stripe GET /v1/subscriptions (live, status=all, paginated)
  -> normalized snapshot and summary
  -> agent formats and sends report
```

## Configuration

Add a `tools.stripe` configuration section with:

- `apiKey`: injected from Coolify's existing `STRIPE_SECRET_KEY`.
- `timeout`: request timeout with a conservative default.
- `maxRetries`: bounded retries for transient failures.

The tool registers only when `apiKey` is non-empty. The key must never appear in tool output or application logs. A restricted Stripe key with read access to subscriptions and customers is preferred, but the existing live secret key remains compatible.

Coolify must expose the key at runtime or render it into nanobot's protected configuration during `init-config`. No key is committed to Git.

## Stripe Data Retrieval

The tool requests `/v1/subscriptions` with `status=all`, `limit=100`, and `expand[]=data.customer`. It follows `has_more` using `starting_after` until all pages have been retrieved.

For every subscription it records:

- subscription ID and Stripe status;
- customer ID, display name, and email when available;
- cancellation flags and relevant period timestamps;
- price/product identifiers;
- recurring interval for every subscription item.

Monthly and annual classification comes from each item's `price.recurring.interval`, not from product names or prices. Subscriptions containing multiple incompatible intervals are classified as `mixed`; missing recurring data is classified as `unknown`.

The response includes counts for every Stripe subscription status: `active`, `trialing`, `past_due`, `unpaid`, `paused`, `canceled`, `incomplete`, and `incomplete_expired`. Unknown future Stripe statuses are retained rather than discarded.

## Tool Contract

The tool takes no arguments for the weekly report. It returns JSON containing:

- `source: "stripe_api_live"`;
- `retrieved_at` as an ISO-8601 UTC timestamp;
- total subscriptions and counts by status and billing interval;
- normalized subscription rows;
- pagination metadata.

The scheduled prompt must explicitly require this tool and must treat `source` and `retrieved_at` as authoritative. It must not claim a live Stripe source unless the tool succeeded during that same job execution.

## Error Handling

Retry only transient network failures, Stripe `429` responses, and `5xx` responses. Honor `Retry-After` when present and use bounded exponential backoff.

Do not retry authentication or authorization failures (`401`/`403`) or other permanent `4xx` responses. Return a sanitized diagnostic containing the HTTP category and Stripe request ID when available, without response bodies that could contain sensitive data.

If any page fails, the entire snapshot fails. Partial counts must not be reported as complete. The agent sends a concise failure notice stating that live Stripe data could not be retrieved; it must not fall back to the May snapshot or any conversation memory.

## Testing

Unit tests use mocked HTTP responses and cover:

- a single page with all supported statuses;
- pagination across multiple pages;
- monthly, annual, mixed, and unknown interval classification;
- expanded and unexpanded customer representations;
- transient retry followed by success;
- non-retryable `401`/`403` handling;
- rate limiting with `Retry-After`;
- failure on a later page without returning partial results;
- redaction of credentials and unsafe Stripe response content.

An integration smoke test, run only when an explicit test key is available, verifies that the tool can list subscriptions without mutating Stripe. Production verification consists of one manual tool invocation followed by observation of the next scheduled job's timestamp and source marker.

## Operational Notes

Remove Stripe from the `mcp-remote@latest` path once the native report tool is enabled. Other MCP servers remain independent. The unrelated MiniMax MCP wheel-installation failure is outside this change and must not prevent the Stripe report tool from registering or running.

Successful completion means the weekly message contains a timestamp from that execution, complete paginated counts derived from Stripe, and no stale-data fallback. A Stripe outage produces an explicit failure message instead of an old report.
