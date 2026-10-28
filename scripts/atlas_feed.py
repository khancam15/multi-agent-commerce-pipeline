"""
atlas_feed.py — Report loop activity to AI Ops Atlas
─────────────────────────────────────────────────────
loop.sh reports every phase result here so Atlas can show real runs for
the pipeline's agents instead of hand-written dates. Only the phase label,
its result and a short detail are sent: never prompts, product copy,
buyer details or credentials.

Run (from loop.sh):
    python scripts/atlas_feed.py ok     "<phase label>" ["<detail>"]
    python scripts/atlas_feed.py failed "<phase label>" ["<detail>"]
    python scripts/atlas_feed.py paused "Loop" ["<detail>"]

Settings read from .env (both optional; without them nothing is sent):
    ATLAS_FEED_URL    — e.g. https://ai-ops-atlas.pages.dev/api/hooks/agents
    ATLAS_FEED_TOKEN  — the key Atlas issued for this pipeline only

Reporting never fails the caller: errors are printed and the exit code is 0.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

PROJECT = "commerce-pipeline"
TIMEOUT_SECS = 5
STATUSES = ("ok", "failed", "skipped", "paused")
CREW = ["niche-scout", "market-analyst", "brand-strategist", "visual-director",
        "copy-strategist", "launch-planner"]

# Phase label prefix (as loop.sh logs it) → Atlas catalog agent ids
_AGENTS: list[tuple[str, list[str]]] = [
    ("Phase 1", CREW),
    ("Phase 3", ["canva-mockups"]),
    ("Phase 5", ["shopify-publisher"]),
    ("Phase 6", ["seo-maintainer"]),
    ("Maintain SEO", ["seo-maintainer"]),
    ("Phase 7", ["health-report"]),
    ("Order delivery", ["order-delivery"]),
    ("Sales sync", ["sales-sync"]),
    ("Email digest", ["email-digest"]),
    ("Database backup", ["db-backup"]),
]


def agents_for(label: str) -> list[str]:
    """Catalog agents a phase belongs to. Anything unlisted is the loop's own work."""
    for prefix, ids in _AGENTS:
        if label.startswith(prefix):
            return ids
    return ["commerce-loop"]


def build_event(status: str, label: str, detail: str = "",
                now: datetime | None = None) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    return {
        "project": PROJECT,
        "agents": agents_for(label),
        "phase": label[:120],
        "status": status,
        "detail": detail[:300],
        "at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
    }


def send(status: str, label: str, detail: str = "") -> bool:
    """POST one event. Returns True when Atlas accepted it, False otherwise."""
    url = os.getenv("ATLAS_FEED_URL", "").strip()
    token = os.getenv("ATLAS_FEED_TOKEN", "").strip()
    if not url or not token:
        return False
    if not url.startswith("https://"):
        print("  [atlas] ATLAS_FEED_URL must start with https:// — not sent")
        return False
    try:
        event = build_event(status, label, detail)
        res = requests.post(url, json=event, timeout=TIMEOUT_SECS,
                            headers={"Authorization": f"Bearer {token}"})
    except (requests.RequestException, ValueError) as exc:
        print(f"  [atlas] not sent: {exc}")
        return False
    if res.status_code >= 300:
        print(f"  [atlas] not sent: HTTP {res.status_code}")
        return False
    return True


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('Usage: python scripts/atlas_feed.py <ok|failed|skipped|paused> "<phase label>" ["<detail>"]')
        return 0
    send(argv[0], argv[1], argv[2] if len(argv) > 2 else "")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
