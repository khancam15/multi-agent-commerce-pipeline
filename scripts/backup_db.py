"""
backup_db.py — Consistent SQLite backups
─────────────────────────────────────────
Copies outputs/pipeline.db to backups/pipeline-YYYYMMDD-HHMMSS.db using
SQLite's online backup API (safe while the pipeline is writing), then
deletes backups older than BACKUP_KEEP_DAYS (default 14).

Run:
    python scripts/backup_db.py

Called by loop.sh once a day. Backups stay on this server: copy the
backups/ folder off the VPS (for example with rclone) so a lost server
does not lose sales and listing history.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import DB_PATH

_ROOT      = Path(__file__).resolve().parent.parent
BACKUP_DIR = _ROOT / "backups"


def backup(db_path: Path = DB_PATH, backup_dir: Path = BACKUP_DIR, keep_days: float | None = None) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    keep_days = float(os.getenv("BACKUP_KEEP_DAYS", "14")) if keep_days is None else keep_days
    backup_dir.mkdir(parents=True, exist_ok=True)

    dest = backup_dir / f"pipeline-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.db"
    src  = sqlite3.connect(db_path)
    dst  = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    cutoff = time.time() - keep_days * 86400
    for old in backup_dir.glob("pipeline-*.db"):
        if old != dest and old.stat().st_mtime < cutoff:
            old.unlink()
    return dest


if __name__ == "__main__":
    try:
        path = backup()
        print(f"  [backup] Saved {path}")
    except Exception as e:
        print(f"[error] Backup failed: {e}")
        sys.exit(1)
