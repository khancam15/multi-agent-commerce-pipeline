import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import alerts
import db
import order_delivery
from common import brand_guide_changed


def _queue(db_path: Path, name: str) -> int:
    return db.insert_queue_item(name, f"{name} title", ["tag"], "desc", 9.0, "Digital", db_path=db_path)


# ── Queue failures ────────────────────────────────────────────────────────────

def test_failed_product_is_marked_failed_after_max_attempts(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    db.init_db(db_path)
    _queue(db_path, "A")

    statuses = [db.record_queue_failure("A", "phase 5", max_attempts=3, db_path=db_path) for _ in range(3)]

    assert statuses == ["pending", "pending", "failed"]
    assert db.get_queue_items("pending", db_path=db_path) == []
    assert db.record_queue_failure("A", "again", db_path=db_path) == ""


def test_failing_product_drops_behind_others(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    db.init_db(db_path)
    _queue(db_path, "A")
    _queue(db_path, "B")

    assert db.next_publishable_product(db_path=db_path) == "A"
    db.record_queue_failure("A", "phase 4.3", db_path=db_path)
    assert db.next_publishable_product(db_path=db_path) == "B"


# ── Template links and deliveries ─────────────────────────────────────────────

def test_template_links_and_delivery_records(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    db.init_db(db_path)
    db.insert_listing("Kit", "Kit title", template_link="https://canva.link/k", status="draft",
                      stripe_product_id="prod_kit", db_path=db_path)
    db.insert_listing("Old", "Old title", stripe_product_id="prod_old", db_path=db_path)

    links = db.template_links_by_product_id(db_path=db_path)
    assert links == {"prod_kit": {"product_name": "Kit", "title": "Kit title",
                                  "template_link": "https://canva.link/k"}}

    assert not db.delivery_exists("cs_1", "prod_kit", db_path=db_path)
    db.record_delivery("cs_1", "#1", "prod_kit", "Kit", "failed", "smtp down", db_path=db_path)
    assert not db.delivery_exists("cs_1", "prod_kit", db_path=db_path)  # failed sends are retried
    db.record_delivery("cs_1", "#1", "prod_kit", "Kit", "sent", db_path=db_path)
    assert db.delivery_exists("cs_1", "prod_kit", db_path=db_path)


def _session(session_id, email, products, payment_status="paid"):
    return {
        "id": session_id,
        "payment_status": payment_status,
        "customer_details": {"email": email},
        "line_items": {"data": [{"id": f"li_{i}", "price": {"product": p}} for i, p in enumerate(products)]},
    }


def test_pending_deliveries_groups_paid_checkouts(monkeypatch) -> None:
    links = {"prod_kit": {"product_name": "Kit", "title": "Kit", "template_link": "https://canva.link/k"}}
    delivered = {("cs_2", "prod_kit")}
    monkeypatch.setattr(order_delivery, "delivery_exists", lambda o, p: (o, p) in delivered)
    sessions = [
        _session("cs_test_aaaa1111", "a@x.test", ["prod_kit", {"id": "prod_kit"}, "prod_other"]),
        _session("cs_2", "b@x.test", ["prod_kit"]),
        _session("cs_3", "c@x.test", ["prod_kit"], payment_status="unpaid"),
    ]

    todo = order_delivery.pending_deliveries(sessions, links)

    assert len(todo) == 1
    assert todo[0]["order_id"] == "cs_test_aaaa1111" and todo[0]["email"] == "a@x.test"
    assert todo[0]["order_name"] == "#AAAA1111"
    assert [i["product_id"] for i in todo[0]["items"]] == ["prod_kit"]


def test_delivery_email_escapes_titles(monkeypatch) -> None:
    monkeypatch.setenv("BRAND_NAME", "Studio")
    subject, body_html, body_text = order_delivery.build_email(
        "#9", [{"title": "<Kit>", "template_link": "https://canva.link/k?a=1&b=2"}]
    )

    assert "#9" in subject
    assert "&lt;Kit&gt;" in body_html and "<Kit>" not in body_html
    assert "https://canva.link/k?a=1&amp;b=2" in body_html
    assert "https://canva.link/k?a=1&b=2" in body_text


# ── Phase 2 refresh ───────────────────────────────────────────────────────────

def test_brand_guide_changed_detects_regenerated_guide() -> None:
    assert not brand_guide_changed({"brand_guide_mtime": 100.0}, 100.0)
    assert brand_guide_changed({"brand_guide_mtime": 100.0}, 200.0)

    started = datetime(2026, 10, 1, tzinfo=UTC)
    legacy = {"started_at": started.replace(tzinfo=None).isoformat()}
    later = (started + timedelta(days=8)).timestamp()
    assert brand_guide_changed(legacy, later)


# ── Alerts ────────────────────────────────────────────────────────────────────

def test_alert_sent_on_second_failure_and_on_recovery(tmp_path, monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(alerts, "STATE_FILE", tmp_path / "alert_state.json")
    monkeypatch.setattr(alerts, "_send", lambda subject, detail: sent.append(subject))
    now = datetime(2026, 10, 28, tzinfo=UTC)

    assert not alerts.record("Phase 5", False, "boom", now=now)
    assert alerts.record("Phase 5", False, "boom", now=now)
    assert not alerts.record("Phase 5", False, "boom", now=now + timedelta(hours=1))
    assert alerts.record("Phase 5", False, "boom", now=now + timedelta(hours=13))
    assert alerts.record("Phase 5", True)
    assert json.loads((tmp_path / "alert_state.json").read_text()) == {}
    assert len(sent) == 3 and sent[-1] == "Phase 5 recovered"


def test_backup_copies_database_and_prunes_old_files(tmp_path) -> None:
    import os
    import time

    import backup_db

    db_path = tmp_path / "pipeline.db"
    db.init_db(db_path)
    _queue(db_path, "A")
    backups = tmp_path / "backups"
    backups.mkdir()
    old = backups / "pipeline-20200101-000000.db"
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 30 * 86400,) * 2)

    dest = backup_db.backup(db_path, backups, keep_days=14)

    assert dest.exists() and not old.exists()
    assert [r["product_name"] for r in db.get_queue_items("pending", db_path=dest)] == ["A"]
