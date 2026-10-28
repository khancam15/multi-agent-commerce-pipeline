"""
seo_analyzer.py — Phase 6
──────────────────────────
Post-publish SEO analysis + auto-apply for the storefront.

Two stages per run:
  ANALYZE  — read this product's current tags, search Google Shopping via
             Serper API for the primary keyword, extract competitor keywords,
             identify gaps, and write the report to the seo_review table.

  APPLY    — build an optimised tag set (keep high-value existing tags, fill
             gaps with top competitor keywords) and save it with an SEO title
             and meta description on the listing. The next storefront build
             uses them for the product page's meta tags and keywords.

The apply stage runs automatically after analysis unless --analyze-only is
passed.

Run:
    python scripts/seo_analyzer.py <product_name>
    python scripts/seo_analyzer.py <product_name> --analyze-only

Requires in .env:
    SERPER_API_KEY          (for Google Shopping keyword research)
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from db import get_listing, insert_seo_review, log_run, update_listing

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

MAX_TAGS     = 250   # upper bound on stored tags
TAG_MAX_LEN  = 255   # longest tag kept
MIN_KEEP_ORIGINAL = 6


# ── Serper competitor research ─────────────────────────────────────────────────

def _get_competitor_keywords(keyword: str, limit: int = 10) -> Counter:
    """
    Search Google Shopping via Serper API for the keyword.
    Extract product titles and tags from top results to build a frequency counter.
    Returns Counter({keyword: frequency}).
    """
    serper_key = os.getenv("SERPER_API_KEY", "")
    if not serper_key:
        print("  [6.3] SERPER_API_KEY not set — skipping competitor research")
        return Counter()

    try:
        resp = requests.post(
            "https://google.serper.dev/shopping",
            headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
            json={"q": keyword, "num": limit},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"  [6.3] Serper request failed: {exc}")
        return Counter()

    results = resp.json().get("shopping", [])

    keyword_counts: Counter = Counter()
    for item in results:
        title = item.get("title", "")
        words = _extract_keywords_from_title(title)
        for w in words:
            keyword_counts[w] += 1

    return keyword_counts


def _extract_keywords_from_title(title: str) -> list[str]:
    """
    Extract useful keyword phrases from a product title.
    Splits on common separators and returns 1-3 word phrases.
    """
    import re
    title_lower = title.lower()
    # Remove special characters, split into words
    words = re.findall(r"[a-z0-9]+(?:'[a-z]+)?", title_lower)

    # Single words
    keywords = list(words)

    # Two-word phrases
    for i in range(len(words) - 1):
        phrase = f"{words[i]} {words[i+1]}"
        if 5 <= len(phrase) <= TAG_MAX_LEN:
            keywords.append(phrase)

    # Three-word phrases
    for i in range(len(words) - 2):
        phrase = f"{words[i]} {words[i+1]} {words[i+2]}"
        if 5 <= len(phrase) <= TAG_MAX_LEN:
            keywords.append(phrase)

    # Filter out very short or stop words
    stop = {"the", "and", "for", "with", "your", "you", "this", "that",
            "are", "not", "from", "its", "has", "get", "our", "use"}
    return [k for k in keywords if k not in stop and len(k) >= 3]


# ── Tag helpers ────────────────────────────────────────────────────────────────

def _clean_tag(tag: object) -> str:
    return " ".join(str(tag).strip().lower().split())


def _normalise_tags(
    tags: Sequence[Any],
    *,
    dedupe: bool = True,
    enforce_length: bool = True,
    max_tags: int | None = None,
) -> list[str]:
    normalised: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags:
        tag = _clean_tag(raw_tag)
        if not tag:
            continue
        if dedupe and tag in seen:
            continue
        if enforce_length and len(tag) > TAG_MAX_LEN:
            continue
        normalised.append(tag)
        seen.add(tag)
        if max_tags is not None and len(normalised) >= max_tags:
            break
    return normalised


def _primary_keyword(title: str) -> str:
    for sep in ["|", "—", "-"]:
        if sep in title:
            phrase = title.split(sep)[0].strip()
            if len(phrase) > 5:
                return phrase
    words = title.split()
    return " ".join(words[:4]) if len(words) >= 4 else title


def _build_optimised_tags(
    current_tags: list[str],
    competitor_counts: Counter,
    target_max: int = 20,
) -> tuple[list[str], list[str], list[str]]:
    """
    Build the best possible tag set (up to target_max) combining existing
    tags with high-frequency competitor keywords.

    Returns (optimised_tags, added_tags, removed_tags).
    Keeps the set focused rather than using every possible keyword.
    """
    current_valid = _normalise_tags(current_tags)
    current_set   = set(current_valid)

    clean_competitor: Counter = Counter()
    for tag, count in competitor_counts.items():
        norm = _normalise_tags([tag])
        if norm:
            clean_competitor[norm[0]] += count

    scored_existing = sorted(
        current_valid,
        key=lambda t: (clean_competitor.get(t, 0) * 2 + 1),
        reverse=True,
    )

    missing_ranked = [
        tag for tag, _ in clean_competitor.most_common()
        if tag not in current_set and len(tag) <= TAG_MAX_LEN
    ]

    kept             = scored_existing[:MIN_KEEP_ORIGINAL]
    remaining_slots  = target_max - len(kept)
    extra_current    = scored_existing[MIN_KEEP_ORIGINAL:target_max]
    fill_from_current = extra_current[:max(0, remaining_slots - len(missing_ranked[:remaining_slots]))]
    fill_from_missing = missing_ranked[:remaining_slots - len(fill_from_current)]

    optimised = (kept + fill_from_current + fill_from_missing)[:target_max]

    added   = [t for t in optimised if t not in current_set]
    removed = [t for t in current_valid if t not in set(optimised)]

    return optimised, added, removed


# ── Listing update ────────────────────────────────────────────────────────────

def _save_seo(product_name: str, tags: list[str], seo_title: str, seo_description: str) -> None:
    """Store optimised tags and meta text on the listing for the next storefront build."""
    update_listing(
        product_name,
        tags=json.dumps(tags),
        seo_title=seo_title[:70],
        seo_description=seo_description[:320],
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def analyze(product_name: str, apply: bool = True) -> int:
    """
    Full Phase 6:
      1. Load the listing (title + current tags) from SQLite
      2. Search top competitor keywords via Serper API
      3. Build optimised tag set
      4. Write gap report to seo_review table
      5. Save optimised tags + SEO title/description on the listing (unless apply=False)

    Returns gap count (number of tags added).
    """
    listing = get_listing(product_name)
    if listing is None:
        raise ValueError(
            f"No published listing found for '{product_name}'. "
            "Run Phase 5 first, or check the listings table."
        )

    title        = listing["title"]
    current_tags = [str(t) for t in json.loads(listing["tags"] or "[]")]
    keyword      = _primary_keyword(title)

    print(f"  [6.2] Current tags ({len(current_tags)}): {current_tags[:8]}")
    print(f"  [6.2] Primary keyword: {keyword!r}")

    # 6.3 — Competitor research via Serper
    print(f"  [6.3] Searching Google Shopping for: {keyword!r}")
    competitor_counts = _get_competitor_keywords(keyword, limit=10)
    top_competitor_tags = [t for t, _ in competitor_counts.most_common(25)]
    print(f"  [6.3] Top competitor keywords: {top_competitor_tags[:10]}")

    # 6.4 — Build optimised tag set
    current_normalised = _normalise_tags(current_tags, dedupe=False, enforce_length=False)
    optimised, added, removed = _build_optimised_tags(current_tags, competitor_counts)
    missing = [t for t in top_competitor_tags if t not in set(current_normalised)]
    tags_changed = optimised != current_normalised

    print(f"  [6.4] Optimised tags ({len(optimised)}): {optimised[:8]}")
    if added:
        print(f"  [6.4] Adding  ({len(added)}): {added[:8]}")
    if removed:
        print(f"  [6.4] Removing({len(removed)}): {removed[:5]}")

    # 6.5 — Write gap report to SQLite
    insert_seo_review(
        product_name=product_name,
        listing_title=title,
        original_tags=current_normalised,
        optimised_tags=optimised,
        added_tags=added,
        removed_tags=removed,
        missing_tags=missing[:20],
        competitor_tags=top_competitor_tags[:25],
    )

    gap_count = len(added)

    # 6.6 — Save to the listing; the storefront build publishes it
    if apply and tags_changed:
        seo_title = f"{title.split('|')[0].strip()} | Editable Canva Template"
        seo_desc  = (f"{title}. Fully editable Canva template, delivered by email after "
                     f"checkout. {', '.join(optimised[:5])}.")
        _save_seo(product_name, optimised, seo_title, seo_desc)
        print(f"  [6.6] ✓ Saved {len(optimised)} tags + SEO title/description to the listing")
        log_run(
            product_name,
            "seo_analyzer",
            "success",
            f"Tags updated — added: {added[:5]}, removed: {removed[:3]}, gap_count: {gap_count}",
        )
    elif not tags_changed:
        print("  [6.6] Tags already optimal — no update needed")
        log_run(product_name, "seo_analyzer", "success", "Tags already optimal, no update needed")
    else:
        print(f"  [6.6] Analyze-only mode — {gap_count} improvement(s) identified but not applied")
        log_run(
            product_name,
            "seo_analyzer",
            "success",
            f"Analyze-only: {gap_count} gaps identified — {missing[:5]}",
        )

    print(f"  [6] SEO gap count: {gap_count}")
    return gap_count


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        print("Usage: python scripts/seo_analyzer.py <product_name> [--analyze-only]")
        sys.exit(1)

    product_name = args[0]
    apply_tags   = "--analyze-only" not in args

    try:
        gap_count = analyze(product_name, apply=apply_tags)
        sys.exit(0)
    except (ValueError, RuntimeError) as e:
        print(f"[error] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[error] Unexpected: {e}")
        sys.exit(1)
