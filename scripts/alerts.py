"""
alerts.py — Failure alerts for the autonomous loop
───────────────────────────────────────────────────
loop.sh reports every phase result here. When a phase fails twice in a
row, an alert email goes to EMAIL_TO, then at most once every
ALERT_REPEAT_HOURS (default 12) while it keeps failing. A success resets
the count and sends a short "recovered" note if an alert had gone out.

Run (from loop.sh):
    python scripts/alerts.py fail "<phase label>" "<detail>"
    python scripts/alerts.py ok   "<phase label>"
    python scripts/alerts.py send "<subject>" "<detail>"   — one-off alert

State lives in outputs/alert_state.json. Alerts never fail the caller:
if email is not configured the alert is only printed.
"""

from __future__ import annotations

import html
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mailer import is_configured, send_email

_ROOT      = Path(__file__).resolve().parent.parent
STATE_FILE = _ROOT / "outputs" / "alert_state.json"
FAIL_THRESHOLD = 2


def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _send(subject: str, detail: str) -> None:
    brand = os.getenv("BRAND_NAME", "Pipeline")
    full  = f"[{brand} pipeline] {subject}"
    to    = os.getenv("EMAIL_TO", "")
    print(f"  [alert] {full}: {detail[:300]}")
    if not (to and is_configured()):
        return
    try:
        send_email(to, full, f"<p>{html.escape(subject)}</p><pre>{html.escape(detail[:4000])}</pre>",
                   f"{subject}\n\n{detail[:4000]}")
    except Exception as exc:  # alerts must never break the loop
        print(f"  [alert] Could not send alert email: {exc}")


def record(label: str, ok: bool, detail: str = "", now: datetime | None = None) -> bool:
    """Update state for one phase result. Returns True if an alert was sent."""
    now    = now or datetime.now(UTC)
    repeat = timedelta(hours=float(os.getenv("ALERT_REPEAT_HOURS", "12")))
    state  = _load()
    entry  = state.get(label, {"failures": 0, "alerted_at": ""})
    sent   = False

    if ok:
        if entry.get("alerted_at"):
            _send(f"{label} recovered", "The phase completed successfully again.")
            sent = True
        state.pop(label, None)
        _save(state)
        return sent

    entry["failures"] = entry.get("failures", 0) + 1
    last = entry.get("alerted_at")
    due  = not last or now - datetime.fromisoformat(last) >= repeat
    if entry["failures"] >= FAIL_THRESHOLD and due:
        _send(f"{label} has failed {entry['failures']} times in a row", detail)
        entry["alerted_at"] = now.isoformat()
        sent = True
    state[label] = entry
    _save(state)
    return sent


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("fail", "ok", "send"):
        print('Usage: alerts.py fail|ok "<label>" ["<detail>"]  |  alerts.py send "<subject>" "<detail>"')
        sys.exit(0)
    if args[0] == "send":
        _send(args[1], args[2] if len(args) > 2 else "")
    else:
        record(args[1], args[0] == "ok", args[2] if len(args) > 2 else "")
    sys.exit(0)
