"""
Shared helpers for the commerce pipeline scripts.
"""

from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def load_brand_guide(brand_guides: tuple[Path, ...]) -> str:
    """Read the generated brand guide from the first existing candidate path."""
    for brand_guide in brand_guides:
        if brand_guide.exists():
            return brand_guide.read_text(encoding="utf-8")

    preferred_file = brand_guides[0]
    fallback_file = brand_guides[1] if len(brand_guides) > 1 else brand_guides[0]
    raise FileNotFoundError(
        f"Brand guide not found at {preferred_file.resolve()} or {fallback_file.resolve()}\n"
        "Run brand_crew.py first to generate it."
    )


def _week_heading_start(text: str, week_number: int) -> int | None:
    patterns = [
        rf"(?im)^\s{{0,3}}#{{1,6}}\s*week\s*{week_number}\b.*$",
        rf"(?im)^\s*week\s*{week_number}\b.*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.start()
    return None


def extract_checklist(brand_guide: str) -> dict[str, list[dict[str, Any]]]:
    """
    Parse week sections from a generated brand guide.
    Returns: {"week_1": [...], ..., "week_4": [...]}.
    """
    starts: list[tuple[int, int]] = []
    for week_number in range(1, 5):
        start = _week_heading_start(brand_guide, week_number)
        if start is not None:
            starts.append((start, week_number))

    starts.sort(key=lambda item: item[0])

    blocks: dict[int, str] = {}
    for i, (start_idx, week_number) in enumerate(starts):
        end_idx = starts[i + 1][0] if i + 1 < len(starts) else len(brand_guide)
        blocks[week_number] = brand_guide[start_idx:end_idx]

    checklist: dict[str, list[dict[str, Any]]] = {}
    for week_number in range(1, 5):
        key = f"week_{week_number}"
        block = blocks.get(week_number, "")
        if not block:
            checklist[key] = []
            continue

        items = re.findall(r"(?m)^\s*(?:\d+\.|[-*])\s+(.+)$", block)
        tool_match = re.search(r"(?im)free\s+tool:\s*(.+)$", block)
        tool = tool_match.group(1).strip().strip("*") if tool_match else None

        checklist[key] = [
            {
                "task": item.strip().replace("**", "").replace("\n", " "),
                "tool": tool,
                "status": "pending",
            }
            for item in items
            if item.strip()
        ]

    total = sum(len(v) for v in checklist.values())
    if total == 0:
        raise ValueError(
            "No checklist tasks could be extracted from brand_guide.md. "
            "Ensure week sections are present (Week 1 ... Week 4) with numbered or bulleted tasks."
        )

    return checklist


def brand_guide_changed(state: dict[str, Any], brand_guide_mtime: float) -> bool:
    """
    True if the brand guide was regenerated after this run state was created.
    Phase 2 then starts a fresh run, so a new brand guide produces new products
    instead of resuming a checklist that is already complete.
    """
    saved = state.get("brand_guide_mtime")
    if saved is not None:
        return float(saved) != float(brand_guide_mtime)
    # Older state files have no mtime: compare against when the run started
    started = state.get("started_at", "")
    try:
        return datetime.fromisoformat(started).timestamp() < brand_guide_mtime
    except ValueError:
        return True


def save_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_state(state_file: Path) -> dict[str, Any] | None:
    if state_file.exists():
        return json.loads(state_file.read_text(encoding="utf-8"))
    return None


def call_with_retry(
    operation: Callable[[], Any],
    *,
    attempts: int = 4,
    base_delay_seconds: float = 1.25,
) -> Any:
    """Retry transient API calls with exponential backoff and jitter."""
    delay = base_delay_seconds
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:  # broad on purpose: Anthropic SDK errors vary by version
            last_error = exc
            if attempt == attempts:
                break
            jitter = random.uniform(0.0, 0.5)
            time.sleep(delay + jitter)
            delay *= 2

    if last_error is not None:
        raise last_error
