from datetime import UTC, datetime
from pathlib import Path
import sys

import pytest
import requests

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import atlas_feed


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status


def test_phase_labels_map_to_catalog_agents() -> None:
    assert atlas_feed.agents_for("Phase 1 (brand builder)") == atlas_feed.CREW
    assert atlas_feed.agents_for("Phase 3B retry (canva product creator)") == ["canva-mockups"]
    assert atlas_feed.agents_for("Phase 5 (stripe publisher)") == ["shopify-publisher"]
    assert atlas_feed.agents_for("Maintain SEO scan") == ["seo-maintainer"]
    assert atlas_feed.agents_for("Order delivery") == ["order-delivery"]
    assert atlas_feed.agents_for("Storefront deploy") == ["commerce-loop"]


def test_event_carries_only_label_status_and_time() -> None:
    now = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    event = atlas_feed.build_event("failed", "Phase 4.1 (image processor)", "Exit 2", now)
    assert event == {
        "project": "commerce-pipeline",
        "agents": ["commerce-loop"],
        "phase": "Phase 4.1 (image processor)",
        "status": "failed",
        "detail": "Exit 2",
        "at": "2026-10-28T09:00:00+00:00",
    }
    with pytest.raises(ValueError):
        atlas_feed.build_event("done", "Loop")


def test_nothing_is_sent_without_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATLAS_FEED_URL", raising=False)
    monkeypatch.delenv("ATLAS_FEED_TOKEN", raising=False)
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("must not send"))
    assert atlas_feed.send("ok", "Loop") is False


def test_plain_http_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_FEED_URL", "http://example.com/api/hooks/agents")
    monkeypatch.setenv("ATLAS_FEED_TOKEN", "t" * 40)
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("must not send"))
    assert atlas_feed.send("ok", "Loop") is False


def test_sends_with_bearer_token_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_FEED_URL", "https://atlas.example/api/hooks/agents")
    monkeypatch.setenv("ATLAS_FEED_TOKEN", "secret-token")
    seen = {}

    def fake_post(url, json, timeout, headers):
        seen.update(url=url, json=json, timeout=timeout, headers=headers)
        return _Resp(202)

    monkeypatch.setattr(requests, "post", fake_post)
    assert atlas_feed.send("ok", "Order delivery") is True
    assert seen["headers"] == {"Authorization": "Bearer secret-token"}
    assert seen["timeout"] == atlas_feed.TIMEOUT_SECS
    assert seen["json"]["agents"] == ["order-delivery"]


def test_errors_never_reach_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_FEED_URL", "https://atlas.example/api/hooks/agents")
    monkeypatch.setenv("ATLAS_FEED_TOKEN", "secret-token")

    def boom(*a, **k):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "post", boom)
    assert atlas_feed.send("ok", "Loop") is False
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(401))
    assert atlas_feed.send("ok", "Loop") is False
    assert atlas_feed.main(["ok", "Loop"]) == 0
    assert atlas_feed.main([]) == 0
