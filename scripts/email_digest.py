"""
email_digest.py — Daily design brief email
────────────────────────────────────────────
Sends a clean daily email with:
  - Product ideas extracted from master.txt (ready to design in Canva)
  - Brand identity summary from brand_guide.md
  - Pipeline health stats from SQLite
  - Queue status (pending / published counts)

Run:
    python scripts/email_digest.py

Called by loop.sh once per day automatically.

Requires in .env:
    EMAIL_TO        — your email address
    EMAIL_FROM      — Gmail address sending from
    EMAIL_SMTP_PASS — Gmail App Password (not your login password)
                      Generate at: myaccount.google.com/apppasswords
"""

from __future__ import annotations

import os
import re
import smtplib
import sys
from datetime import UTC, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from db import get_conn, get_sales_summary, log_run

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

MASTER_FILE      = _ROOT / "outputs" / "master.txt"
BRAND_GUIDE_FILE = _ROOT / "outputs" / "brand_guide.md"

EMAIL_TO        = os.getenv("EMAIL_TO", "")
EMAIL_FROM      = os.getenv("EMAIL_FROM", "")
EMAIL_SMTP_PASS = os.getenv("EMAIL_SMTP_PASS", "")
SMTP_HOST       = "smtp.gmail.com"
SMTP_PORT       = 587


def _utc_now() -> datetime:
    """Return UTC time in the same naive ISO format used by existing rows."""
    return datetime.now(UTC).replace(tzinfo=None)


# ── Parsers ───────────────────────────────────────────────────────────────────

def _parse_products(master_text: str) -> list[dict]:
    """Extract product names and prices from Week 2 listing section."""
    products = []
    # Match lines like: 1. UGC Pitch Deck ($19)
    pattern = re.compile(r"\d+\.\s+(.+?)\s+\(\$(\d+(?:\.\d{2})?)\)")
    for match in pattern.finditer(master_text):
        products.append({
            "name":  match.group(1).strip(),
            "price": match.group(2),
        })
    return products[:10]  # cap at 10 products


def _parse_brand_summary(brand_text: str) -> dict:
    """Pull store name, tagline, niche, aesthetic from brand_guide.md."""
    summary = {}

    for label, patterns in {
        "store_name":  [r"Store Name[:\s]+(.+)", r"# (.+)"],
        "tagline":     [r"Tagline[:\s]+(.+)"],
        "niche":       [r"CHOSEN NICHE[:\s]+(.+)", r"Niche[:\s]+(.+)"],
        "aesthetic":   [r"AESTHETIC[:\s]+(.+)", r"Aesthetic[:\s]+(.+)"],
        "colors":      [r"BRAND COLORS?[:\s]*\n((?:[-•]\s*.+\n?)+)"],
    }.items():
        for p in patterns:
            m = re.search(p, brand_text, re.IGNORECASE)
            if m:
                summary[label] = m.group(1).strip()[:120]
                break

    return summary


def _get_stats() -> dict:
    """Pull this week's published/failed counts and queue status."""
    week_start = (_utc_now() - timedelta(days=7)).isoformat()
    stats = {}

    with get_conn() as conn:
        stats["published_week"] = conn.execute(
            "SELECT COUNT(*) FROM run_log WHERE status='success' AND phase IN ('shopify_uploader', 'stripe_publisher') AND run_at >= ?",
            (week_start,)
        ).fetchone()[0]

        stats["failed_week"] = conn.execute(
            "SELECT COUNT(*) FROM run_log WHERE status='failed' AND run_at >= ?",
            (week_start,)
        ).fetchone()[0]

        stats["queue_pending"] = conn.execute(
            "SELECT COUNT(*) FROM queue WHERE status='pending'"
        ).fetchone()[0]

        stats["total_published"] = conn.execute(
            "SELECT COUNT(*) FROM listings"
        ).fetchone()[0]

    return stats


# ── Email builder ─────────────────────────────────────────────────────────────

def _build_email(brand: dict, products: list[dict], stats: dict, sales: dict, weekly_limit: int = 5) -> tuple[str, str]:
    """Return (subject, html_body)."""
    date_str = _utc_now().strftime("%B %d, %Y")
    subject  = f"Commerce Pipeline — Design Brief for {date_str}"

    store    = brand.get("store_name", "Your Store")
    tagline  = brand.get("tagline", "")
    niche    = brand.get("niche", "")
    aesthetic = brand.get("aesthetic", "")

    # ── Product rows ──────────────────────────────────────────────────────────
    if products:
        product_rows = "".join(
            f"""<tr>
                  <td style="padding:8px 12px;border-bottom:1px solid #eee;">{p['name']}</td>
                  <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center;">${p['price']}</td>
                  <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#888;font-size:12px;">
                    Automated: Phase 3 generates mockups via Canva MCP
                  </td>
                </tr>"""
            for p in products
        )
        products_section = f"""
        <h2 style="color:#2C3E50;margin-top:32px;">Products to Design in Canva</h2>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
          <thead>
            <tr style="background:#f5f5f5;">
              <th style="padding:8px 12px;text-align:left;">Product</th>
              <th style="padding:8px 12px;text-align:center;">Price</th>
              <th style="padding:8px 12px;text-align:left;">Action</th>
            </tr>
          </thead>
          <tbody>{product_rows}</tbody>
        </table>
        <p style="color:#888;font-size:12px;margin-top:8px;">
          Full titles, tags, and descriptions are in <code>outputs/master.txt</code> on the VPS.
        </p>"""
    else:
        products_section = """
        <h2 style="color:#2C3E50;margin-top:32px;">Products</h2>
        <p style="color:#888;">Run Phase 2 to generate product ideas: <code>./run.sh phase2</code></p>"""

    # ── Stats pills ───────────────────────────────────────────────────────────
    pill = lambda color, label, val: (
        f'<span style="background:{color};color:#fff;border-radius:12px;'
        f'padding:4px 12px;margin-right:8px;font-size:13px;">'
        f'{label}: <strong>{val}</strong></span>'
    )

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:24px;color:#333;">

  <div style="background:#2C3E50;color:#fff;padding:20px 24px;border-radius:8px;">
    <h1 style="margin:0;font-size:22px;">{store}</h1>
    <p style="margin:4px 0 0;color:#DAA520;font-size:14px;">{tagline}</p>
  </div>

  <h2 style="color:#2C3E50;margin-top:28px;">Revenue (Last 7 Days)</h2>
  <div style="margin-bottom:8px;">
    {pill('#27ae60', 'Net Revenue', f"${sales['total_revenue']:.2f}")}
    {pill('#16a085', 'Orders', sales['order_count'])}
    {pill('#2980b9', 'Units Sold', sales['units_sold'])}
    {pill('#8e44ad', 'All-Time', f"${sales['all_time_revenue']:.2f}")}
  </div>
  {"<p style='font-size:13px;margin-top:6px;'>🏆 Best seller: <strong>" + sales['best_product'] + f"</strong> (${sales['best_product_revenue']:.2f})</p>" if sales['best_product'] else ""}

  <h2 style="color:#2C3E50;margin-top:28px;">Pipeline Health (Last 7 Days)</h2>
  <div style="margin-bottom:16px;">
    {pill('#27ae60', 'Published', stats['published_week'])}
    {pill('#e74c3c', 'Failed', stats['failed_week'])}
    {pill('#f39c12', 'In Queue', stats['queue_pending'])}
    {pill('#2980b9', 'Total Live', stats['total_published'])}
  </div>

  <h2 style="color:#2C3E50;margin-top:28px;">Brand Brief</h2>
  <table style="font-size:14px;border-collapse:collapse;width:100%;">
    <tr><td style="padding:6px 12px;color:#888;width:120px;">Niche</td>
        <td style="padding:6px 12px;">{niche}</td></tr>
    <tr style="background:#f9f9f9;">
        <td style="padding:6px 12px;color:#888;">Aesthetic</td>
        <td style="padding:6px 12px;">{aesthetic}</td></tr>
  </table>

  {products_section}

  <h2 style="color:#2C3E50;margin-top:32px;">Pipeline Status</h2>
  <p style="font-size:14px;color:#27ae60;">✦ Fully autonomous — no manual action needed.</p>
  <ul style="font-size:14px;line-height:2;">
    <li>Phase 3 designs 6 mockup images per product (Canva MCP)</li>
    <li>Phase 3B creates the actual template buyers download (Canva MCP)</li>
    <li>Phase 4 processes images and builds the listing</li>
    <li>Phase 5 saves products to Stripe as drafts (up to {weekly_limit}/week)</li>
    <li>Phase 6 runs SEO gap analysis after each publish</li>
  </ul>
  <p style="font-size:13px;color:#888;">
    To adjust pacing: edit <code>WEEKLY_PUBLISH_LIMIT</code> and
    <code>PUBLISH_COOLDOWN_HOURS</code> in your <code>.env</code> file.
  </p>

  <hr style="border:none;border-top:1px solid #eee;margin:32px 0;">
  <p style="color:#aaa;font-size:11px;text-align:center;">
    Commerce Pipeline · {date_str} · Auto-generated
  </p>

</body>
</html>"""

    return subject, html


# ── Sender ────────────────────────────────────────────────────────────────────

def send_digest() -> None:
    if not all([EMAIL_TO, EMAIL_FROM, EMAIL_SMTP_PASS]):
        print("  [email_digest] Skipping — EMAIL_TO, EMAIL_FROM, or EMAIL_SMTP_PASS not set in .env")
        return

    master_text = MASTER_FILE.read_text(encoding="utf-8") if MASTER_FILE.exists() else ""
    brand_text  = BRAND_GUIDE_FILE.read_text(encoding="utf-8") if BRAND_GUIDE_FILE.exists() else ""

    brand    = _parse_brand_summary(brand_text)
    products = _parse_products(master_text)
    stats    = _get_stats()

    weekly_limit = int(os.getenv("WEEKLY_PUBLISH_LIMIT", "5"))
    sales        = get_sales_summary(days=7)
    subject, html_body = _build_email(brand, products, stats, sales, weekly_limit)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_SMTP_PASS)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    print(f"  [email_digest] Sent to {EMAIL_TO}")
    log_run("pipeline", "email_digest", "success", f"Digest sent to {EMAIL_TO}")


if __name__ == "__main__":
    try:
        send_digest()
    except Exception as e:
        print(f"  [email_digest] ERROR: {e}")
        log_run("pipeline", "email_digest", "failed", str(e))
        sys.exit(1)
