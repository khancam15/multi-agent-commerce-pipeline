import json
from collections import Counter
from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import seo_analyzer


def test_normalise_tags_strips_lowercases_dedupes_and_enforces_limits() -> None:
    tags = [
        " Canva Template ",
        "canva   template",
        "",
        "x" * 256,
        "SEO Planner",
    ]

    assert seo_analyzer._normalise_tags(tags) == ["canva template", "seo planner"]


def test_build_optimised_tags_returns_clean_limited_tag_set() -> None:
    current = [
        " Canva Template ",
        "canva template",
        "seller workbook",
        "x" * 256,
    ]
    competitor_counts = Counter(
        {
            "Canva Template": 4,
            "Digital Planner": 3,
            "UGC Kit": 2,
            "x" * 256: 10,
        }
    )

    optimised, added, removed = seo_analyzer._build_optimised_tags(current, competitor_counts)

    assert optimised == ["canva template", "seller workbook", "digital planner", "ugc kit"]
    assert added == ["digital planner", "ugc kit"]
    assert len(optimised) <= seo_analyzer.MAX_TAGS
    assert all(len(tag) <= seo_analyzer.TAG_MAX_LEN for tag in optimised)


def test_competitor_keywords_extracts_google_shopping_terms(monkeypatch) -> None:
    class FakeResponse:
        ok = True

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "shopping": [
                    {"title": "Canva Template Digital Planner"},
                    {"title": "Digital Planner SEO Kit"},
                ]
            }

    monkeypatch.setenv("SERPER_API_KEY", "serper")
    monkeypatch.setattr(seo_analyzer.requests, "post", lambda *args, **kwargs: FakeResponse())

    counts = seo_analyzer._get_competitor_keywords("planner")

    assert counts["digital"] == 2
    assert counts["digital planner"] == 2
    assert counts["seo kit"] == 1


def test_analyze_applies_cleanup_only_tag_changes(monkeypatch) -> None:
    updates: list[dict] = []

    monkeypatch.setattr(
        seo_analyzer,
        "get_listing",
        lambda product_name: {
            "title": "Canva Template | Seller Kit",
            "tags": json.dumps([" Canva Template ", "canva template"]),
        },
    )
    monkeypatch.setattr(
        seo_analyzer,
        "_get_competitor_keywords",
        lambda keyword, limit=10: Counter({"canva template": 5}),
    )
    monkeypatch.setattr(seo_analyzer, "insert_seo_review", lambda **kwargs: None)
    monkeypatch.setattr(seo_analyzer, "log_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        seo_analyzer,
        "update_listing",
        lambda product_name, **fields: updates.append(fields),
    )

    gap_count = seo_analyzer.analyze("Test Product", apply=True)

    assert gap_count == 0
    assert len(updates) == 1
    assert json.loads(updates[0]["tags"]) == ["canva template"]
    assert updates[0]["seo_title"] == "Canva Template | Editable Canva Template"
    assert len(updates[0]["seo_description"]) <= 320
