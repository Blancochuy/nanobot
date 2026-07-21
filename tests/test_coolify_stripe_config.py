"""Deployment contract for live Stripe reporting in Coolify."""

from pathlib import Path


def test_coolify_uses_native_stripe_report_config():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert '"stripe": {' in compose
    assert '"apiKey": "$${STRIPE_SECRET_KEY}"' in compose
    assert '"timeout": 20' in compose
    assert '"maxRetries": 2' in compose
    assert '"STRIPE_AUTH_HEADER"' not in compose
    assert '"https://mcp.stripe.com/"' not in compose
