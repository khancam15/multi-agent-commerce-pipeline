"""
shopify_setup.py  —  One-time Shopify credential setup
───────────────────────────────────────────────────────
Verifies your Shopify Admin API credentials and saves them to .env.
Run this once on the 💻 HOST before running any pipeline phases.

Steps:
  1. Reads SHOPIFY_STORE_DOMAIN + SHOPIFY_ACCESS_TOKEN from .env
     (or prompts you to enter them if missing)
  2. Calls GET /shop.json to verify credentials
  3. Saves verified values to .env

Shopify credential setup:
  1. Go to your Shopify Admin → Settings → Apps and sales channels
  2. Click "Develop apps" → Create an app
  3. Under "Configuration" → enable Admin API scopes:
       write_products, read_products,
       read_orders, write_orders,
       read_inventory
  4. Install the app → copy the "Admin API access token" (shpat_...)
  5. Your store domain is your-store.myshopify.com

Run:
    💻 HOST: python scripts/shopify_setup.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests
from dotenv import dotenv_values, load_dotenv

from shopify_common import api_version

_ROOT    = Path(__file__).resolve().parent.parent
ENV_PATH = _ROOT / ".env"
load_dotenv(ENV_PATH)

API_VERSION = api_version()


def _normalize_domain(domain: str) -> str:
    return domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")


def _load_env() -> dict[str, str]:
    return {k: v for k, v in dotenv_values(ENV_PATH).items() if v is not None}


def _save_env(updates: dict[str, str]) -> None:
    env = _load_env()
    env.update(updates)
    lines = []
    for k, v in env.items():
        lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _prompt(label: str, current: str) -> str:
    if current:
        print(f"  {label}: [current: {current[:20]}...] (press Enter to keep)")
        val = input(f"  New {label}: ").strip()
        return val or current
    val = input(f"  {label}: ").strip()
    return val


def verify_credentials(domain: str, access_token: str) -> dict:
    """Call GET /shop.json to verify credentials. Returns shop data."""
    domain = _normalize_domain(domain)
    url  = f"https://{domain}/admin/api/{API_VERSION}/shop.json"
    hdrs = {"X-Shopify-Access-Token": access_token}
    try:
        resp = requests.get(url, headers=hdrs, timeout=15)
    except requests.ConnectionError:
        raise RuntimeError(f"Cannot connect to {domain} — check your store domain.")
    if resp.status_code == 401:
        raise RuntimeError("Invalid access token — check SHOPIFY_ACCESS_TOKEN.")
    if resp.status_code == 404:
        raise RuntimeError(f"Store not found: {domain} — check SHOPIFY_STORE_DOMAIN.")
    resp.raise_for_status()
    return resp.json().get("shop", {})


def main() -> None:
    print("\n  ── Shopify API Setup ─────────────────────────────────")
    print("  Verifies Admin API credentials and saves them to .env\n")

    env = _load_env()

    domain       = env.get("SHOPIFY_STORE_DOMAIN", "")
    access_token = env.get("SHOPIFY_ACCESS_TOKEN", "")

    if not domain:
        print("  Enter your Shopify store domain (e.g. my-store.myshopify.com):")
        domain = input("  Domain: ").strip()
    if not access_token:
        print("\n  Enter your Admin API access token (shpat_...):")
        access_token = input("  Access token: ").strip()

    if not domain or not access_token:
        print("[error] Both SHOPIFY_STORE_DOMAIN and SHOPIFY_ACCESS_TOKEN are required.")
        sys.exit(1)

    domain = _normalize_domain(domain)

    print(f"\n  Verifying credentials for: {domain}")
    try:
        shop = verify_credentials(domain, access_token)
    except RuntimeError as exc:
        print(f"\n[error] {exc}")
        sys.exit(1)

    shop_name = shop.get("name", domain)
    currency  = shop.get("currency", "USD")
    plan      = shop.get("plan_name", "unknown")

    print(f"\n  ✓ Connected to shop: {shop_name}")
    print(f"    Currency: {currency}")
    print(f"    Plan:     {plan}")

    _save_env({
        "SHOPIFY_STORE_DOMAIN": domain,
        "SHOPIFY_ACCESS_TOKEN": access_token,
    })
    print("\n  ✓ Credentials saved to .env")
    print("  You can now run: ./run.sh phase5 <product_name>\n")


if __name__ == "__main__":
    main()
