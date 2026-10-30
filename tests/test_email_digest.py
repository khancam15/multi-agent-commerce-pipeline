import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import email_digest

SALES = {"total_revenue": 0.0, "order_count": 0, "units_sold": 0, "all_time_revenue": 0.0,
         "best_product": "", "best_product_revenue": 0.0}
STATS = {"published_week": 0, "failed_week": 0, "queue_pending": 0, "total_published": 0}


def _point_at(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(email_digest, "PAUSE_FILE", tmp_path / "PAUSE")
    monkeypatch.setattr(email_digest, "ALERT_STATE_FILE", tmp_path / "alert_state.json")
    monkeypatch.setattr(email_digest, "BACKUP_DIR", tmp_path / "backups")


def test_loop_status_reads_pause_failures_and_backup_age(tmp_path, monkeypatch) -> None:
    _point_at(tmp_path, monkeypatch)
    (tmp_path / "PAUSE").touch()
    (tmp_path / "alert_state.json").write_text(json.dumps(
        {"Database backup": {"failures": 1, "alerted_at": ""}, "Phase 5": {"failures": 0}}))
    (tmp_path / "backups").mkdir()
    backup = tmp_path / "backups" / "pipeline-20261028-000000.db"
    backup.touch()
    made = datetime(2026, 10, 28, tzinfo=UTC).timestamp()
    os.utime(backup, (made, made))

    status = email_digest._loop_status(now=datetime(2026, 10, 30, tzinfo=UTC))

    assert status["paused"] is True
    assert status["failing"] == ["Database backup (1x)"]
    assert status["backup_age_hours"] == 48.0
    line = email_digest._status_line(status)
    assert "PAUSED" in line and "Database backup (1x)" in line and "overdue" in line


def test_status_line_flags_missing_backup(tmp_path, monkeypatch) -> None:
    _point_at(tmp_path, monkeypatch)
    status = email_digest._loop_status(now=datetime(2026, 10, 30, tzinfo=UTC))
    assert not status["paused"] and status["failing"] == []
    assert "NO database backup found" in email_digest._status_line(status)


def test_digest_shows_status_line_at_top() -> None:
    status = {"paused": False, "failing": [], "drafts": 2, "backup_age_hours": 3.0}
    _, body = email_digest._build_email({}, [], STATS, SALES, 5, status)
    assert body.index("Loop running") < body.index("Revenue (Last 7 Days)")
    assert "2 draft(s) awaiting ./run.sh approve" in body
    assert "no manual action needed" not in body
