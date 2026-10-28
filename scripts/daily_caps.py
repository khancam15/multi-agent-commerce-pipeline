"""
daily_caps.py — Hard daily limits on spend and sends
────────────────────────────────────────────────────
A runaway loop or a bad deploy could call the model or send email without
end. These counters stop that in code, on top of the provider's own limits:

    MAX_MODEL_RUNS_PER_DAY  (default 96)  model-driven scripts started by
                                          run.sh / loop.sh (scoped_env.py)
    MAX_EMAILS_PER_DAY      (default 100) buyer and digest emails (mailer.py)
    MAX_ALERTS_PER_DAY      (default 20)  failure alerts (mailer.py)

Counts reset at midnight UTC and live in logs/daily_caps.json. A run past its
cap fails, so loop.sh records the failure and alerts as usual; a buyer email
past its cap is recorded as failed and retried after the reset.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = _ROOT / "logs" / "daily_caps.json"

DEFAULTS = {"model_run": ("MAX_MODEL_RUNS_PER_DAY", 96),
            "email": ("MAX_EMAILS_PER_DAY", 100),
            "alert": ("MAX_ALERTS_PER_DAY", 20)}


class DailyCapReached(RuntimeError):
    pass


def limit_for(kind: str) -> int:
    var, default = DEFAULTS[kind]
    # Environment first, then .env (read, not loaded, so no secret enters os.environ)
    raw = (os.getenv(var) or dotenv_values(_ROOT / ".env").get(var) or "").strip()
    return int(raw) if raw.isdigit() else default


def take(kind: str, now: datetime | None = None, state_file: Path | None = None) -> bool:
    """Count one `kind` for today. False (and nothing counted) once today's cap is reached."""
    path = state_file or STATE_FILE
    today = (now or datetime.now(UTC)).date().isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        try:
            state = json.loads(fh.read() or "{}")
        except json.JSONDecodeError:
            state = {}
        if state.get("date") != today:
            state = {"date": today, "counts": {}}
        counts = state.setdefault("counts", {})
        if counts.get(kind, 0) >= limit_for(kind):
            return False
        counts[kind] = counts.get(kind, 0) + 1
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(state))
    return True


def require(kind: str) -> None:
    if not take(kind):
        var, _ = DEFAULTS[kind]
        raise DailyCapReached(f"Daily cap reached for {kind} ({limit_for(kind)}; set {var} to change)")
