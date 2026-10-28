"""
order_delivery.py — Post-purchase template delivery
────────────────────────────────────────────────────
Emails each buyer the Canva "Use template" link for every pipeline
product in a paid Stripe checkout. The link is never shown on the
storefront, so this is the only way buyers receive what they paid for.

Run:
    python scripts/order_delivery.py              — deliver new paid checkouts
    python scripts/order_delivery.py --dry-run    — show what would be sent
    python scripts/order_delivery.py --days 30    — look back further (default 7)

Called by loop.sh every cycle and every DELIVERY_INTERVAL seconds while idle.

Each checkout line is delivered once (tracked in the deliveries table).
Failed sends are retried on the next run.

Requires:
    STRIPE_SECRET_KEY — see stripe_common.py
    EMAIL_FROM, EMAIL_SMTP_PASS — see mailer.py
Optional:
    SUPPORT_EMAIL — reply address shown to buyers (defaults to EMAIL_FROM)
"""

from __future__ import annotations

import html
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from db import (
    delivery_exists,
    init_db,
    log_run,
    record_delivery,
    template_links_by_product_id,
)
from mailer import is_configured, send_email
from stripe_common import line_item_product_id, paid_checkout_sessions

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


def build_email(order_name: str, items: list[dict[str, str]]) -> tuple[str, str, str]:
    """Return (subject, html, text) for one order's delivery email."""
    brand   = os.getenv("BRAND_NAME", "Your Brand")
    support = os.getenv("SUPPORT_EMAIL") or os.getenv("EMAIL_FROM", "")
    subject = f"Your templates from {brand} (order {order_name})"

    rows_html = "".join(
        f'<li><strong>{html.escape(i["title"])}</strong><br>'
        f'<a href="{html.escape(i["template_link"], quote=True)}">Open your copy in Canva</a></li>'
        for i in items
    )
    body_html = (
        f"<p>Thank you for your order {html.escape(order_name)}.</p>"
        f"<p>Click each link below. Canva will make your own editable copy of the template.</p>"
        f"<ul>{rows_html}</ul>"
        f"<p>Canva is free to use. Questions? Reply to this email"
        f"{' or write to ' + html.escape(support) if support else ''}.</p>"
        f"<p>{html.escape(brand)}</p>"
    )
    body_text = (
        f"Thank you for your order {order_name}.\n\n"
        "Open each link to get your own editable copy in Canva:\n\n"
        + "\n".join(f"- {i['title']}: {i['template_link']}" for i in items)
        + f"\n\nCanva is free to use. Questions? Reply to this email.\n\n{brand}\n"
    )
    return subject, body_html, body_text


def pending_deliveries(
    sessions: list[dict[str, Any]],
    links: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """
    Group undelivered pipeline products by paid checkout session.
    Returns [{order_id, order_name, email, items: [{product_id, product_name, title, template_link}]}].
    """
    result: list[dict[str, Any]] = []
    for session in sessions:
        if session.get("payment_status") not in ("paid", "no_payment_required"):
            continue
        session_id = str(session.get("id", ""))
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for line in (session.get("line_items") or {}).get("data", []):
            product_id = line_item_product_id(line)
            if product_id in seen or product_id not in links:
                continue
            seen.add(product_id)
            if delivery_exists(session_id, product_id):
                continue
            items.append({"product_id": product_id, **links[product_id]})
        if items:
            details = session.get("customer_details") or {}
            result.append({
                "order_id": session_id,
                "order_name": f"#{session_id[-8:].upper()}",
                "email": details.get("email") or session.get("customer_email") or "",
                "items": items,
            })
    return result


def deliver(days: int = 7, dry_run: bool = False) -> int:
    """Send delivery emails. Returns the number of orders that failed."""
    init_db()
    links = template_links_by_product_id()
    if not links:
        print("  [delivery] No published products with template links yet")
        return 0
    if not dry_run and not is_configured():
        raise RuntimeError("EMAIL_FROM and EMAIL_SMTP_PASS are required to deliver templates")

    since    = datetime.now(UTC) - timedelta(days=days)
    sessions = list(paid_checkout_sessions(since))
    todo     = pending_deliveries(sessions, links)
    print(f"  [delivery] {len(sessions)} paid checkout(s) in the last {days} days, "
          f"{len(todo)} need delivery")

    failures = 0
    for d in todo:
        names = ", ".join(i["product_name"] for i in d["items"])
        if not d["email"]:
            print(f"  [delivery] {d['order_name']}: no buyer email on the checkout — skipping")
            for i in d["items"]:
                record_delivery(d["order_id"], d["order_name"], i["product_id"],
                                i["product_name"], "no_email", "Checkout has no email address")
            failures += 1
            continue
        if dry_run:
            print(f"  [delivery] Would email {d['order_name']}: {names}")
            continue
        subject, body_html, body_text = build_email(d["order_name"], d["items"])
        try:
            send_email(d["email"], subject, body_html, body_text)
            status, detail = "sent", ""
            print(f"  [delivery] Sent {d['order_name']}: {names}")
        except Exception as exc:  # record and retry next run
            status, detail = "failed", str(exc)
            failures += 1
            print(f"  [delivery] FAILED {d['order_name']}: {exc}")
        for i in d["items"]:
            record_delivery(d["order_id"], d["order_name"], i["product_id"],
                            i["product_name"], status, detail)

    if not dry_run:
        log_run("pipeline", "order_delivery", "failed" if failures else "success",
                f"orders={len(todo)}, failures={failures}")
    return failures


if __name__ == "__main__":
    args = sys.argv[1:]
    lookback = 7
    if "--days" in args:
        lookback = int(args[args.index("--days") + 1])
    try:
        failed = deliver(days=lookback, dry_run="--dry-run" in args)
    except Exception as e:
        print(f"[error] {e}")
        sys.exit(1)
    sys.exit(1 if failed else 0)
