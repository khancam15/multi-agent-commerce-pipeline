"""
shopify_common.py — Shared Shopify Admin API settings
──────────────────────────────────────────────────────
One place for the API version and request helpers used by every script
that talks to the Shopify Admin API.

Shopify supports each API version for about 12 months. After that it
silently serves the oldest supported version instead, so keep
DEFAULT_API_VERSION current (or set SHOPIFY_API_VERSION in .env).
Release calendar: https://shopify.dev/docs/api/usage/versioning
"""

from __future__ import annotations

import os

DEFAULT_API_VERSION = "2026-07"


def api_version() -> str:
    return os.getenv("SHOPIFY_API_VERSION") or DEFAULT_API_VERSION


def normalize_domain(domain: str) -> str:
    return domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")


def api_base(domain: str) -> str:
    return f"https://{normalize_domain(domain)}/admin/api/{api_version()}"


def headers(access_token: str) -> dict[str, str]:
    return {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
