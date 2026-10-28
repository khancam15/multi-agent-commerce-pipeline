"""
stripe_setup.py — One-time Stripe setup check
──────────────────────────────────────────────
Saves your Stripe secret key to .env (if it is not there yet) and checks
that it works.

Run:
    python scripts/stripe_setup.py      or     ./run.sh stripe-auth

Get the key at https://dashboard.stripe.com/test/apikeys (test mode) or
https://dashboard.stripe.com/apikeys (live). Start with the test key: test
purchases use card 4242 4242 4242 4242 and move no money.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv, set_key

sys.path.insert(0, str(Path(__file__).parent))
_ROOT    = Path(__file__).resolve().parent.parent
ENV_PATH = _ROOT / ".env"
load_dotenv(ENV_PATH)

from stripe_common import StripeError, is_test_mode, request  # after .env is loaded


def main() -> int:
    key = (dotenv_values(ENV_PATH).get("STRIPE_SECRET_KEY") or "").strip()
    if not key:
        key = getpass.getpass("Paste your Stripe secret key (sk_test_... or sk_live_...): ").strip()
        if not key.startswith(("sk_", "rk_")):
            print("[error] That does not look like a secret key (it should start with sk_ or rk_).")
            return 1
        ENV_PATH.touch(exist_ok=True)
        set_key(str(ENV_PATH), "STRIPE_SECRET_KEY", key)
        import os
        os.environ["STRIPE_SECRET_KEY"] = key
        print("  Saved STRIPE_SECRET_KEY to .env")

    try:
        request("GET", "/products", params={"limit": 1}, label="Stripe setup check")
    except StripeError as exc:
        print(f"[error] {exc}")
        return 1

    mode = "TEST mode — no real money moves" if is_test_mode() else "LIVE mode — real payments"
    print(f"  Stripe key works ({mode}).")
    print("  Next: set STOREFRONT_BASE_URL in .env, then run ./run.sh check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
