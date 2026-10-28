from datetime import UTC, datetime
from pathlib import Path
import sys

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import daily_caps
import mailer

DAY1 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
DAY2 = datetime(2026, 10, 29, 0, 1, tzinfo=UTC)


def test_cap_stops_at_the_limit_and_resets_next_day(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MAX_EMAILS_PER_DAY", "2")
    state = tmp_path / "caps.json"
    assert daily_caps.take("email", DAY1, state)
    assert daily_caps.take("email", DAY1, state)
    assert not daily_caps.take("email", DAY1, state)
    assert daily_caps.take("email", DAY2, state)


def test_kinds_are_counted_separately(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MAX_EMAILS_PER_DAY", "1")
    monkeypatch.setenv("MAX_ALERTS_PER_DAY", "1")
    state = tmp_path / "caps.json"
    assert daily_caps.take("email", DAY1, state)
    assert daily_caps.take("alert", DAY1, state)
    assert not daily_caps.take("email", DAY1, state)


def test_bad_or_missing_limit_uses_the_default(monkeypatch) -> None:
    monkeypatch.setenv("MAX_MODEL_RUNS_PER_DAY", "lots")
    assert daily_caps.limit_for("model_run") == 96
    monkeypatch.delenv("MAX_MODEL_RUNS_PER_DAY")
    assert daily_caps.limit_for("model_run") == 96


def test_corrupt_state_file_starts_fresh(tmp_path: Path) -> None:
    state = tmp_path / "caps.json"
    state.write_text("{not json")
    assert daily_caps.take("email", DAY1, state)


def test_mailer_refuses_past_the_cap_without_connecting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMAIL_FROM", "shop@example.com")
    monkeypatch.setenv("EMAIL_SMTP_PASS", "x")
    monkeypatch.setattr(daily_caps, "STATE_FILE", tmp_path / "caps.json")
    monkeypatch.setenv("MAX_EMAILS_PER_DAY", "0")

    def no_smtp(*_a, **_k):
        raise AssertionError("SMTP must not be reached past the cap")

    monkeypatch.setattr(mailer.smtplib, "SMTP", no_smtp)
    with pytest.raises(daily_caps.DailyCapReached):
        mailer.send_email("buyer@example.com", "Your template", "<p>hi</p>")
