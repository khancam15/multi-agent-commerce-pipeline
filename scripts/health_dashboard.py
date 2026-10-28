"""
health_dashboard.py  —  Phase 7
──────────────────────────────────
Daily summary and health check. Queries SQLite and prints a structured
report: listings published this week, failure rate, average run duration,
and any queue rows pending for over 48 hours or marked failed.

Run:
    python scripts/health_dashboard.py

Called by n8n daily summary workflow (Phase 7.5 / 7.6).
Output is plain text — n8n captures stdout and sends via notification node.

Execution flow (per v3 Phase 7 spec):
  7.5  Query run_log: listings published this week, failure rate, counts
  7.6  Flag queue rows pending for > 48 hours or marked failed
  7.7  Print structured summary for n8n notification
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from db import get_conn, get_sales_summary, init_db, DB_PATH

# Type annotation for get_sales_summary

_ROOT = Path(__file__).resolve().parent.parent
STUCK_THRESHOLD_HOURS = 48


def _week_start() -> str:
    """ISO timestamp for Monday 00:00:00 of the current week."""
    today = datetime.utcnow()
    monday = today - timedelta(days=today.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def generate_report() -> str:
    """
    Queries SQLite for weekly stats and stuck-queue items.
    Returns a formatted plain-text report string.
    """
    init_db()  # adds any new tables/columns to an older database
    week_start = _week_start()
    lines: list[str] = []

    with get_conn() as conn:

        # Published this week
        published = conn.execute(
            "SELECT COUNT(*) FROM run_log WHERE status = 'success' AND phase IN ('shopify_uploader', 'stripe_publisher') AND run_at >= ?",
            (week_start,),
        ).fetchone()[0]

        # Failed this week
        failed = conn.execute(
            "SELECT COUNT(*) FROM run_log WHERE status = 'failed' AND run_at >= ?",
            (week_start,),
        ).fetchone()[0]

        total_runs = published + failed
        failure_rate = f"{(failed / total_runs * 100):.1f}%" if total_runs > 0 else "n/a"

        # Recent failures
        recent_failures = conn.execute(
            """SELECT product_name, phase, message, run_at
               FROM run_log WHERE status = 'failed' AND run_at >= ?
               ORDER BY run_at DESC LIMIT 5""",
            (week_start,),
        ).fetchall()

        # Stuck queue items (pending for > 48 hours, or given up after repeated failures)
        cutoff = (datetime.utcnow() - timedelta(hours=STUCK_THRESHOLD_HOURS)).isoformat()
        stuck = conn.execute(
            """SELECT id, product_name, status, updated_at FROM queue
               WHERE (status = 'pending' AND created_at < ?) OR status = 'failed'
               ORDER BY updated_at""",
            (cutoff,),
        ).fetchall()

        # Drafts waiting for review, and buyers whose template email failed
        drafts = conn.execute(
            "SELECT product_name FROM listings WHERE status = 'draft'"
        ).fetchall()
        undelivered = conn.execute(
            "SELECT order_name, product_name, status FROM deliveries WHERE status != 'sent'"
        ).fetchall()

        # Total published all time
        total_published = conn.execute(
            "SELECT COUNT(*) FROM listings"
        ).fetchone()[0]

    # Sales & revenue
    sales = get_sales_summary(days=7)

    lines.append("=" * 50)
    lines.append("  Commerce Pipeline — Daily Health Dashboard")
    lines.append(f"  Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    lines.append("=" * 50)
    lines.append("")
    lines.append("  ── Revenue (last 7 days) ──────────────────")
    lines.append(f"  Net revenue (est.):   ${sales['total_revenue']:.2f}")
    lines.append(f"  Gross revenue:        ${sales['gross_revenue']:.2f}")
    lines.append(f"  Orders:               {sales['order_count']}")
    lines.append(f"  Units sold:           {sales['units_sold']}")
    if sales["best_product"]:
        lines.append(f"  Best product:         {sales['best_product']} (${sales['best_product_revenue']:.2f})")
    lines.append(f"  All-time revenue:     ${sales['all_time_revenue']:.2f}")
    lines.append("")
    lines.append("  ── Pipeline ───────────────────────────────")
    lines.append(f"  Published this week:  {published}")
    lines.append(f"  Failed this week:     {failed}")
    lines.append(f"  Failure rate:         {failure_rate}")
    lines.append(f"  Total published:      {total_published}")
    lines.append("")

    if recent_failures:
        lines.append("  Recent Failures:")
        for row in recent_failures:
            lines.append(f"    [{row['run_at'][:16]}] {row['product_name']} / {row['phase']}: {row['message'][:80]}")
        lines.append("")

    if drafts:
        lines.append(f"  Drafts waiting for review ({len(drafts)}) — ./run.sh approve <product>:")
        for row in drafts:
            lines.append(f"    {row['product_name']}")
        lines.append("")

    if undelivered:
        lines.append(f"  ⚠ Buyers not yet sent their template ({len(undelivered)}):")
        for row in undelivered:
            lines.append(f"    {row['order_name']}: {row['product_name']} ({row['status']})")
        lines.append("")

    if stuck:
        lines.append(f"  ⚠ Stuck queue items (pending > {STUCK_THRESHOLD_HOURS}h or failed):")
        for row in stuck:
            lines.append(f"    ID {row['id']}: {row['product_name']} — {row['status']} (since {row['updated_at'][:16]})")
        lines.append("")
    else:
        lines.append("  Queue: no stuck items")
        lines.append("")

    lines.append("=" * 50)
    return "\n".join(lines)


if __name__ == "__main__":
    if not DB_PATH.exists():
        print("[health_dashboard] No database found — run db.py first to initialise.")
    else:
        print(generate_report())
