"""
stripe_common.py — Shared Stripe API helpers
─────────────────────────────────────────────
Thin wrapper around the Stripe REST API used by publishing, approval,
order delivery and sales sync. Uses plain HTTPS requests, so no Stripe SDK
is needed.

Requires in .env:
    STRIPE_SECRET_KEY — sk_test_... while testing, sk_live_... to sell.
                        A restricted key works if it can write Products,
                        Prices and Payment Links and read Checkout Sessions.
Optional:
    STRIPE_API_VERSION — pin an API version; defaults to your account's version
    STORE_CURRENCY     — three-letter currency code, default usd

Every write sends an Idempotency-Key, so Stripe applies a retried request
only once. That makes it safe to retry after a timeout.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from api_retry import retry_request

API_BASE = "https://api.stripe.com/v1"


class StripeError(RuntimeError):
    pass


def secret_key() -> str:
    return os.getenv("STRIPE_SECRET_KEY", "").strip()


def is_test_mode() -> bool:
    return secret_key().startswith(("sk_test_", "rk_test_"))


def currency() -> str:
    return (os.getenv("STORE_CURRENCY") or "usd").strip().lower()


def to_minor_units(amount: float) -> int:
    """Dollars (or other major unit) to cents, as Stripe expects."""
    return int(round(float(amount) * 100))


def form_encode(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Flatten nested dicts and lists into Stripe's bracketed form fields."""
    pairs: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, dict):
            pairs.extend(form_encode(value, name))
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    pairs.extend(form_encode(item, f"{name}[{i}]"))
                else:
                    pairs.append((f"{name}[{i}]", _scalar(item)))
        else:
            pairs.append((name, _scalar(value)))
    return pairs


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    label: str = "",
) -> dict[str, Any]:
    """Call the Stripe API and return the decoded JSON. Raises StripeError on failure."""
    key = secret_key()
    if not key:
        raise StripeError("STRIPE_SECRET_KEY is not set in .env")

    headers = {"Authorization": f"Bearer {key}"}
    if os.getenv("STRIPE_API_VERSION"):
        headers["Stripe-Version"] = os.environ["STRIPE_API_VERSION"]
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    resp = retry_request(
        method,
        f"{API_BASE}{path}",
        headers=headers,
        params=form_encode(params) if params else None,
        data=form_encode(data) if data else None,
        timeout=30,
        _label=label or f"Stripe {method} {path}",
        # With an idempotency key Stripe ignores repeats, so retries are safe
        _idempotent=True if idempotency_key else None,
    )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not resp.ok:
        message = body.get("error", {}).get("message") or resp.text[:300]
        raise StripeError(f"Stripe {method} {path} failed ({resp.status_code}): {message}")
    return body


def paid_checkout_sessions(since: datetime, expand: list[str] | None = None) -> Iterator[dict[str, Any]]:
    """
    Yield completed, paid Checkout Sessions created since `since`, newest first.
    Payment Link purchases are Checkout Sessions, so this covers every sale.
    """
    params: dict[str, Any] = {
        "status": "complete",
        "limit": 100,
        "created": {"gte": int(since.astimezone(UTC).timestamp())},
        "expand": ["data.line_items", *(expand or [])],
    }
    while True:
        page = request("GET", "/checkout/sessions", params=params, label="Stripe checkout sessions")
        sessions = page.get("data", [])
        for session in sessions:
            if session.get("payment_status") in ("paid", "no_payment_required"):
                yield session
        if not page.get("has_more") or not sessions:
            return
        params["starting_after"] = sessions[-1]["id"]


def line_item_product_id(line_item: dict[str, Any]) -> str:
    """Stripe product ID for a Checkout line item (price.product may be expanded or an ID)."""
    product = (line_item.get("price") or {}).get("product") or ""
    return product.get("id", "") if isinstance(product, dict) else str(product)
