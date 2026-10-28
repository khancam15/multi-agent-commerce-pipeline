"""
db.py — SQLite schema and helpers
───────────────────────────────────
Single source of truth for all database operations.
Tables:
  queue       — product work queue (pending → published, or failed after
                QUEUE_MAX_ATTEMPTS failed publish attempts)
  listings    — published products, with the private template link
  run_log     — timestamped execution record per product
  seo_review  — post-publish tag gap analysis results
  sales       — Shopify order line-item revenue
  deliveries  — template links emailed to buyers (one row per order + product)
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = _ROOT / "outputs" / "pipeline.db"

SEO_REVIEW_JSON_COLUMNS = (
    "original_tags",
    "optimised_tags",
    "added_tags",
    "removed_tags",
)

LISTINGS_OPTIONAL_COLUMNS = {
    "shopify_url": "TEXT",
    "shopify_product_id": "TEXT",
    "template_link": "TEXT",
    "status": "TEXT",
}

QUEUE_OPTIONAL_COLUMNS = {
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "last_error": "TEXT",
}

RUN_LOG_OPTIONAL_COLUMNS = {
    "shopify_url": "TEXT",
}


def _utc_now() -> datetime:
    """Return UTC time in the same naive ISO format used by existing rows."""
    return datetime.now(UTC).replace(tzinfo=None)


@contextmanager
def get_conn(db_path: Path = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    """Create all four tables if they don't exist. Safe to call on every startup."""
    db_path.parent.mkdir(exist_ok=True)
    with get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS queue (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name    TEXT    NOT NULL,
                title           TEXT    NOT NULL,
                tags            TEXT    NOT NULL,  -- JSON array
                description     TEXT    NOT NULL,
                price           REAL    NOT NULL,
                category        TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'pending',
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS listings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name    TEXT    NOT NULL,
                title           TEXT    NOT NULL UNIQUE,
                shopify_url     TEXT,
                shopify_product_id TEXT,
                published_at    TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS run_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name    TEXT    NOT NULL,
                phase           TEXT    NOT NULL,
                status          TEXT    NOT NULL,  -- success / failed / skipped
                message         TEXT,
                shopify_url     TEXT,
                run_at          TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS seo_review (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name    TEXT    NOT NULL,
                listing_title   TEXT    NOT NULL,
                original_tags   TEXT    NOT NULL DEFAULT '[]',  -- JSON array
                optimised_tags  TEXT    NOT NULL DEFAULT '[]',  -- JSON array
                added_tags      TEXT    NOT NULL DEFAULT '[]',  -- JSON array
                removed_tags    TEXT    NOT NULL DEFAULT '[]',  -- JSON array
                missing_tags    TEXT    NOT NULL,  -- JSON array
                competitor_tags TEXT    NOT NULL,  -- JSON array
                gap_count       INTEGER NOT NULL,
                reviewed_at     TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sales (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id  TEXT    NOT NULL UNIQUE,
                listing_id      TEXT,
                product_name    TEXT,
                title           TEXT,
                amount          REAL    NOT NULL,  -- revenue after fees
                gross_amount    REAL    NOT NULL,  -- buyer paid
                quantity        INTEGER NOT NULL DEFAULT 1,
                currency        TEXT    NOT NULL DEFAULT 'USD',
                sale_date       TEXT    NOT NULL,
                fetched_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deliveries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id        TEXT    NOT NULL,
                order_name      TEXT,
                product_id      TEXT    NOT NULL,
                product_name    TEXT,
                status          TEXT    NOT NULL,  -- sent / failed / no_email
                detail          TEXT,
                delivered_at    TEXT    NOT NULL,
                UNIQUE(order_id, product_id)
            );
        """)
        _ensure_optional_columns(conn, "listings", LISTINGS_OPTIONAL_COLUMNS)
        _ensure_optional_columns(conn, "queue", QUEUE_OPTIONAL_COLUMNS)
        _ensure_optional_columns(conn, "run_log", RUN_LOG_OPTIONAL_COLUMNS)
        _ensure_seo_review_columns(conn)


def _ensure_optional_columns(
    conn: sqlite3.Connection,
    table_name: str,
    columns: dict[str, str],
) -> None:
    """Add optional columns to existing tables without rewriting data."""
    existing_columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }

    for column, column_type in columns.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {column_type}")


def _ensure_seo_review_columns(conn: sqlite3.Connection) -> None:
    """Add audit columns to existing seo_review tables without losing old rows."""
    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(seo_review)").fetchall()
    }

    for column in SEO_REVIEW_JSON_COLUMNS:
        if column not in existing_columns:
            conn.execute(
                f"ALTER TABLE seo_review ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]'"
            )


# ── Queue helpers ─────────────────────────────────────────────────────────────

def insert_queue_item(
    product_name: str,
    title: str,
    tags: list[str],
    description: str,
    price: float,
    category: str,
    db_path: Path = DB_PATH,
) -> int:
    import json
    now = _utc_now().isoformat()
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO queue
               (product_name, title, tags, description, price, category, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (product_name, title, json.dumps(tags), description, price, category, now, now),
        )
        return cur.lastrowid


def get_queue_items(status: str, db_path: Path = DB_PATH) -> list[sqlite3.Row]:
    with get_conn(db_path) as conn:
        return conn.execute(
            "SELECT * FROM queue WHERE status = ? ORDER BY created_at ASC", (status,)
        ).fetchall()


def update_queue_status(item_id: int, status: str, db_path: Path = DB_PATH) -> None:
    now = _utc_now().isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE queue SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, item_id),
        )


def update_queue_status_by_name(product_name: str, status: str, db_path: Path = DB_PATH) -> None:
    """Update the most recent queue row for a product by name."""
    now = _utc_now().isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """UPDATE queue SET status = ?, updated_at = ?
               WHERE id = (
                   SELECT id FROM queue WHERE product_name = ?
                   ORDER BY id DESC LIMIT 1
               )""",
            (status, now, product_name),
        )


QUEUE_MAX_ATTEMPTS = 3


def record_queue_failure(
    product_name: str,
    error: str,
    max_attempts: int = QUEUE_MAX_ATTEMPTS,
    db_path: Path = DB_PATH,
) -> str:
    """
    Count a failed publish attempt for the newest pending row of a product.
    After max_attempts the row is marked 'failed' so it stops blocking the
    queue. Returns the row's new status ('pending', 'failed', or '' if no row).
    """
    now = _utc_now().isoformat()
    with get_conn(db_path) as conn:
        _ensure_optional_columns(conn, "queue", QUEUE_OPTIONAL_COLUMNS)
        row = conn.execute(
            """SELECT id, COALESCE(attempts, 0) AS attempts FROM queue
               WHERE product_name = ? AND status = 'pending'
               ORDER BY id DESC LIMIT 1""",
            (product_name,),
        ).fetchone()
        if row is None:
            return ""
        attempts = row["attempts"] + 1
        status = "failed" if attempts >= max_attempts else "pending"
        conn.execute(
            "UPDATE queue SET attempts = ?, last_error = ?, status = ?, updated_at = ? WHERE id = ?",
            (attempts, error[:500], status, now, row["id"]),
        )
        return status


def next_publishable_product(db_path: Path = DB_PATH) -> str:
    """
    Name of the product to publish next: the pending row with the fewest
    failed attempts, oldest first. A product that keeps failing therefore
    drops behind the others instead of blocking them.
    """
    with get_conn(db_path) as conn:
        _ensure_optional_columns(conn, "queue", QUEUE_OPTIONAL_COLUMNS)
        row = conn.execute(
            """SELECT product_name FROM queue WHERE status = 'pending'
               ORDER BY COALESCE(attempts, 0) ASC, created_at ASC LIMIT 1"""
        ).fetchone()
    return row["product_name"] if row else ""


# ── Weekly pacing helpers ─────────────────────────────────────────────────────

def count_published_this_week(db_path: Path = DB_PATH) -> int:
    """Count listings published since Monday 00:00 UTC of the current week."""
    today    = _utc_now()
    monday   = today - timedelta(days=today.weekday())
    week_start = monday.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM listings WHERE published_at >= ?",
            (week_start,),
        ).fetchone()
    return row["cnt"] if row else 0


def hours_since_last_publish(db_path: Path = DB_PATH) -> float:
    """Hours elapsed since the most recent listing was published. Returns 9999 if never."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT published_at FROM listings ORDER BY published_at DESC LIMIT 1"
        ).fetchone()
    if not row or not row["published_at"]:
        return 9999.0
    last = datetime.fromisoformat(row["published_at"])
    return (_utc_now() - last).total_seconds() / 3600


def seconds_until_week_reset(db_path: Path = DB_PATH) -> int:
    """Seconds until next Monday 00:00 UTC (when the weekly publish counter resets)."""
    now    = _utc_now()
    days_ahead = (7 - now.weekday()) % 7 or 7          # 1-7
    next_monday = (now + timedelta(days=days_ahead)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(3600, int((next_monday - now).total_seconds()))


# ── Listings (dedupe) helpers ─────────────────────────────────────────────────

def title_exists(title: str, db_path: Path = DB_PATH) -> bool:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM listings WHERE title = ?", (title,)
        ).fetchone()
        return row is not None


def insert_listing(
    product_name: str,
    title: str,
    shopify_url: str | None,
    shopify_product_id: str | int | None = None,
    template_link: str | None = None,
    status: str | None = None,
    db_path: Path = DB_PATH,
) -> None:
    now = _utc_now().isoformat()
    with get_conn(db_path) as conn:
        _ensure_optional_columns(conn, "listings", LISTINGS_OPTIONAL_COLUMNS)
        conn.execute(
            """INSERT INTO listings
               (product_name, title, shopify_url, shopify_product_id,
                template_link, status, published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(title) DO UPDATE SET
                   product_name = excluded.product_name,
                   shopify_url = excluded.shopify_url,
                   shopify_product_id = excluded.shopify_product_id,
                   template_link = COALESCE(excluded.template_link, listings.template_link),
                   status = COALESCE(excluded.status, listings.status),
                   published_at = excluded.published_at""",
            (product_name, title, shopify_url, str(shopify_product_id or ""),
             template_link or None, status, now),
        )


def set_listing_status(product_name: str, status: str, db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        _ensure_optional_columns(conn, "listings", LISTINGS_OPTIONAL_COLUMNS)
        conn.execute(
            "UPDATE listings SET status = ? WHERE product_name = ?",
            (status, product_name),
        )


def template_links_by_product_id(db_path: Path = DB_PATH) -> dict[str, dict[str, str]]:
    """Map Shopify product ID → {product_name, title, template_link} for delivery."""
    with get_conn(db_path) as conn:
        _ensure_optional_columns(conn, "listings", LISTINGS_OPTIONAL_COLUMNS)
        rows = conn.execute(
            """SELECT shopify_product_id, product_name, title, template_link FROM listings
               WHERE COALESCE(shopify_product_id, '') != ''
                 AND COALESCE(template_link, '') != ''"""
        ).fetchall()
    return {
        r["shopify_product_id"]: {
            "product_name": r["product_name"],
            "title": r["title"],
            "template_link": r["template_link"],
        }
        for r in rows
    }


# ── Delivery helpers ──────────────────────────────────────────────────────────

def delivery_exists(order_id: str, product_id: str, db_path: Path = DB_PATH) -> bool:
    """True if this order line was already delivered (or permanently skipped)."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM deliveries WHERE order_id = ? AND product_id = ?",
            (order_id, product_id),
        ).fetchone()
    return row is not None and row["status"] != "failed"


def record_delivery(
    order_id: str,
    order_name: str,
    product_id: str,
    product_name: str,
    status: str,
    detail: str = "",
    db_path: Path = DB_PATH,
) -> None:
    now = _utc_now().isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO deliveries
               (order_id, order_name, product_id, product_name, status, detail, delivered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(order_id, product_id) DO UPDATE SET
                   status = excluded.status,
                   detail = excluded.detail,
                   delivered_at = excluded.delivered_at""",
            (order_id, order_name, product_id, product_name, status, detail[:500], now),
        )


# ── Run log helpers ───────────────────────────────────────────────────────────

def log_run(
    product_name: str,
    phase: str,
    status: str,
    message: str = "",
    shopify_url: str | None = None,
    db_path: Path = DB_PATH,
) -> None:
    now = _utc_now().isoformat()
    with get_conn(db_path) as conn:
        _ensure_optional_columns(conn, "run_log", RUN_LOG_OPTIONAL_COLUMNS)
        conn.execute(
            "INSERT INTO run_log (product_name, phase, status, message, shopify_url, run_at) VALUES (?, ?, ?, ?, ?, ?)",
            (product_name, phase, status, message, shopify_url, now),
        )


# ── SEO review helpers ────────────────────────────────────────────────────────

def insert_seo_review(
    product_name: str,
    listing_title: str,
    missing_tags: list[str],
    competitor_tags: list[str],
    original_tags: list[str] | None = None,
    optimised_tags: list[str] | None = None,
    added_tags: list[str] | None = None,
    removed_tags: list[str] | None = None,
    db_path: Path = DB_PATH,
) -> None:
    import json
    now = _utc_now().isoformat()
    gap_count = len(added_tags) if added_tags is not None else len(missing_tags)
    with get_conn(db_path) as conn:
        _ensure_seo_review_columns(conn)
        conn.execute(
            """INSERT INTO seo_review
               (product_name, listing_title, original_tags, optimised_tags,
                added_tags, removed_tags, missing_tags, competitor_tags, gap_count, reviewed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                product_name,
                listing_title,
                json.dumps(original_tags or []),
                json.dumps(optimised_tags or []),
                json.dumps(added_tags or []),
                json.dumps(removed_tags or []),
                json.dumps(missing_tags),
                json.dumps(competitor_tags),
                gap_count,
                now,
            ),
        )


# ── Sales helpers ─────────────────────────────────────────────────────────────

def upsert_sale(
    transaction_id: str,
    listing_id: str,
    product_name: str,
    title: str,
    amount: float,
    gross_amount: float,
    quantity: int,
    currency: str,
    sale_date: str,
    db_path: Path = DB_PATH,
) -> bool:
    """
    Insert a sale row. Skips silently if transaction_id already exists.
    Returns True if a new row was inserted.
    """
    now = _utc_now().isoformat()
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO sales
               (transaction_id, listing_id, product_name, title,
                amount, gross_amount, quantity, currency, sale_date, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (transaction_id, listing_id, product_name, title,
             amount, gross_amount, quantity, currency, sale_date, now),
        )
        return cur.rowcount > 0


def get_sales_summary(days: int = 7, db_path: Path = DB_PATH) -> dict:
    """
    Return a summary dict for the last `days` days:
      total_revenue, gross_revenue, order_count, units_sold,
      best_product (name), best_product_revenue
    """
    since = (_utc_now() - timedelta(days=days)).isoformat()
    with get_conn(db_path) as conn:
        row = conn.execute(
            """SELECT
                 COALESCE(SUM(amount), 0)       AS total_revenue,
                 COALESCE(SUM(gross_amount), 0) AS gross_revenue,
                 COUNT(*)                        AS order_count,
                 COALESCE(SUM(quantity), 0)      AS units_sold
               FROM sales WHERE sale_date >= ?""",
            (since,),
        ).fetchone()

        best = conn.execute(
            """SELECT product_name, SUM(amount) AS rev
               FROM sales WHERE sale_date >= ?
               GROUP BY product_name ORDER BY rev DESC LIMIT 1""",
            (since,),
        ).fetchone()

        all_time = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM sales"
        ).fetchone()[0]

    return {
        "period_days":          days,
        "total_revenue":        round(float(row["total_revenue"]), 2),
        "gross_revenue":        round(float(row["gross_revenue"]), 2),
        "order_count":          int(row["order_count"]),
        "units_sold":           int(row["units_sold"]),
        "best_product":         best["product_name"] if best else None,
        "best_product_revenue": round(float(best["rev"]), 2) if best else 0.0,
        "all_time_revenue":     round(float(all_time), 2),
    }


def get_latest_sale_date(db_path: Path = DB_PATH) -> str | None:
    """Return the sale_date of the most recent synced transaction, or None."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT sale_date FROM sales ORDER BY sale_date DESC LIMIT 1"
        ).fetchone()
    return row["sale_date"] if row else None


if __name__ == "__main__":
    init_db()
    print(f"Database initialised at: {DB_PATH}")
