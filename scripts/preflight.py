"""
preflight.py — Token pre-flight check
───────────────────────────────────────
Validates that all required API credentials are set and working before
the loop does any real work. Runs once at loop.sh startup.

Checks (in order):
  1. Required .env keys are present and non-empty
  2. Shopify Admin API token is valid (GET /admin/api/.../shop.json)
  3. Canva access token is valid (GET /v1/users/me)
     → auto-refreshes if expired, saves new tokens to .env
  4. ANTHROPIC_API_KEY is present (not validated with a live call — saves cost)

Exit codes:
  0  — all checks passed (or non-fatal warnings only)
  1  — one or more required credentials are missing/unfixable

Designed to fail fast with a clear message so you know exactly what to
fix before the pipeline burns an hour on Phases 1–3B then crashes at Phase 5.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
ENV_PATH = _ROOT / ".env"

sys.path.insert(0, str(Path(__file__).parent))
from api_retry import rget, rpost
from shopify_common import api_version

CANVA_ME_URL   = "https://api.canva.com/rest/v1/users/me"
CANVA_TOKEN_URL = "https://api.canva.com/rest/v1/oauth/token"
SHOPIFY_API_VERSION = api_version()


# ── .env helpers ──────────────────────────────────────────────────────────────

def _load() -> dict[str, str]:
    return {k: v for k, v in dotenv_values(ENV_PATH).items() if v is not None}


def _normalize_domain(domain: str) -> str:
    return domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")


def _save(updates: dict[str, str]) -> None:
    env = _load()
    env.update(updates)
    ENV_PATH.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_required_keys(env: dict) -> list[str]:
    """Return list of missing required keys."""
    required = [
        "ANTHROPIC_API_KEY",
        "SHOPIFY_STORE_DOMAIN",
        "SHOPIFY_ACCESS_TOKEN",
        "CANVA_ACCESS_TOKEN",
        "CANVA_REFRESH_TOKEN",
    ]
    return [k for k in required if not env.get(k, "").strip()]


def _check_shopify(env: dict) -> tuple[bool, str]:
    """
    Ping the Shopify Admin API with the configured access token.
    Returns (ok, message).
    """
    domain = _normalize_domain(env["SHOPIFY_STORE_DOMAIN"])
    url = f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/shop.json"
    resp = rget(
        url,
        headers={"X-Shopify-Access-Token": env["SHOPIFY_ACCESS_TOKEN"]},
        timeout=15,
        _label="Shopify preflight",
    )

    if resp.status_code == 200:
        shop = resp.json().get("shop", {})
        name = shop.get("name") or domain
        return True, f"Shopify token valid ({name}) ✓"
    if resp.status_code == 401:
        return False, "Shopify access token invalid — re-run: python scripts/shopify_setup.py"
    if resp.status_code == 404:
        return False, f"Shopify store not found: {domain}"

    return False, f"Shopify API unexpected response ({resp.status_code}): {resp.text[:120]}"


def _check_canva(env: dict) -> tuple[bool, str]:
    """
    Validate Canva access token via GET /v1/users/me.
    Attempts one token refresh on 401 before giving up.
    Returns (ok, message).
    """
    hdrs = {"Authorization": f"Bearer {env['CANVA_ACCESS_TOKEN']}"}

    resp = rget(CANVA_ME_URL, headers=hdrs, timeout=15, _label="Canva preflight")

    if resp.status_code == 200:
        display = resp.json().get("team", {}).get("display_name", "")
        suffix  = f" ({display})" if display else ""
        return True, f"Canva token valid{suffix} ✓"

    if resp.status_code == 401:
        print("  [preflight] Canva token expired — refreshing...")
        client_id     = env.get("CANVA_CLIENT_ID", "")
        client_secret = env.get("CANVA_CLIENT_SECRET", "")

        if not client_id or not client_secret:
            return False, (
                "Canva token expired but CANVA_CLIENT_ID/SECRET not set — "
                "re-run: python scripts/canva_oauth.py"
            )
        try:
            r = rpost(
                CANVA_TOKEN_URL,
                data={
                    "grant_type":    "refresh_token",
                    "client_id":     client_id,
                    "client_secret": client_secret,
                    "refresh_token": env["CANVA_REFRESH_TOKEN"],
                },
                timeout=30,
                _label="Canva token refresh",
            )
            if r.ok:
                data = r.json()
                env["CANVA_ACCESS_TOKEN"]  = data["access_token"]
                env["CANVA_REFRESH_TOKEN"] = data.get("refresh_token", env["CANVA_REFRESH_TOKEN"])
                _save({
                    "CANVA_ACCESS_TOKEN":  env["CANVA_ACCESS_TOKEN"],
                    "CANVA_REFRESH_TOKEN": env["CANVA_REFRESH_TOKEN"],
                })
                return True, "Canva token refreshed and saved ✓"
            return False, f"Canva token refresh failed ({r.status_code}): {r.text[:120]}"
        except Exception as exc:
            return False, f"Canva token refresh error: {exc}"

    if resp.status_code == 404:
        # /users/me not available on this token scope — token is still valid
        return True, "Canva token present (scope check skipped) ✓"

    return False, f"Canva API unexpected response ({resp.status_code}): {resp.text[:120]}"


# ── Main ──────────────────────────────────────────────────────────────────────

def run_preflight() -> bool:
    """
    Run all pre-flight checks.
    Returns True if the pipeline should proceed, False if it should abort.
    Prints a clear status line for each check.
    """
    print("[preflight] Running credential checks...")
    env     = _load()
    fatal   = False

    # ── 1. Required keys present ─────────────────────────────────────────────
    missing = _check_required_keys(env)
    if missing:
        for key in missing:
            print(f"  [preflight] MISSING: {key} — add to .env")
        print(f"  [preflight] {len(missing)} required key(s) missing. Aborting.")
        return False
    print("  [preflight] Required keys present ✓")

    # ── 2. Anthropic API key (presence only — live call costs money) ─────────
    ak = env.get("ANTHROPIC_API_KEY", "")
    if ak.startswith("sk-ant-"):
        print("  [preflight] Anthropic API key format valid ✓")
    else:
        print("  [preflight] WARNING: ANTHROPIC_API_KEY doesn't look right (expected sk-ant-...)")

    # ── 3. Shopify token ─────────────────────────────────────────────────────
    shopify_ok, shopify_msg = _check_shopify(env)
    print(f"  [preflight] Shopify: {shopify_msg}")
    if not shopify_ok:
        fatal = True

    # ── 4. Canva token ───────────────────────────────────────────────────────
    canva_ok, canva_msg = _check_canva(env)
    print(f"  [preflight] Canva: {canva_msg}")
    if not canva_ok:
        # Canva failure is non-fatal for the loop overall — Phase 3/3B will
        # fail on their own with a clear message, but Phases 1/2/4/5 still work.
        print("  [preflight] WARNING: Canva auth failed — Phase 3/3B will not run.")

    # ── Result ───────────────────────────────────────────────────────────────
    if fatal:
        print("[preflight] FATAL: Shopify credentials invalid. Fix them before restarting.")
        return False

    print("[preflight] Pre-flight passed — starting pipeline.")
    return True


# Exit codes: 0 passed, 1 credentials invalid (needs a person),
# 75 network error (temporary — safe to retry later)
EXIT_TEMPFAIL = 75

if __name__ == "__main__":
    import requests

    try:
        ok = run_preflight()
    except requests.RequestException as exc:
        print(f"[preflight] Network error — could not reach an API: {exc}")
        sys.exit(EXIT_TEMPFAIL)
    sys.exit(0 if ok else 1)
