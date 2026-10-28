"""
scoped_env.py — Run one pipeline script with only the secrets it needs
──────────────────────────────────────────────────────────────────────
Every script reads the same .env, so by default each process (including the
CrewAI and Claude phases that read web pages) holds the Stripe key, the Gmail
app password and the Canva tokens. With AGENT_ENV_SCOPING=true in .env,
run.sh and loop.sh start each script through this wrapper, which passes only
that script's secrets and blanks the rest. A blank value stays blank: the
script's own load_dotenv(".env") never overrides a variable that is already
set, so the withheld keys can't come back.

Run (from run.sh and loop.sh):
    python scripts/scoped_env.py scripts/<name>.py [args...]

Without AGENT_ENV_SCOPING=true the script runs with the environment unchanged.

This keeps secrets out of process environments, logs and anything a model or
tool can read from os.environ. It does not stop code from opening .env
itself; for that, run the loop as a non-root user and give each agent its own
user and env file (docs/least-privilege.md).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values

_ROOT = Path(__file__).resolve().parent.parent

CANVA = frozenset({"CANVA_CLIENT_ID", "CANVA_CLIENT_SECRET", "CANVA_ACCESS_TOKEN",
                   "CANVA_REFRESH_TOKEN", "CANVA_MCP_TOKEN"})

# Every credential in .env.example. Anything not listed here (brand name,
# limits, URLs, model names) is a setting and passes through unchanged.
SECRET_KEYS = frozenset({"ANTHROPIC_API_KEY", "SERPER_API_KEY", "STRIPE_SECRET_KEY",
                         "EMAIL_SMTP_PASS", "ATLAS_FEED_TOKEN"}) | CANVA

NONE: frozenset[str] = frozenset()

# The secrets each script (and the modules it imports) actually reads.
AGENT_SECRETS: dict[str, frozenset[str]] = {
    # Checks that verify every credential
    "preflight.py":             SECRET_KEYS,
    "health_check.py":          SECRET_KEYS,
    # Model-driven phases
    "brand_crew.py":            frozenset({"ANTHROPIC_API_KEY", "SERPER_API_KEY"}),
    "listing_generator.py":     frozenset({"ANTHROPIC_API_KEY"}),
    "canva_image_generator.py": frozenset({"ANTHROPIC_API_KEY"}) | CANVA,
    "canva_product_creator.py": frozenset({"ANTHROPIC_API_KEY"}) | CANVA,
    "canva_oauth.py":           CANVA,
    "seo_analyzer.py":          frozenset({"SERPER_API_KEY"}),
    # Stripe
    "stripe_setup.py":          frozenset({"STRIPE_SECRET_KEY"}),
    "stripe_publisher.py":      frozenset({"STRIPE_SECRET_KEY"}),
    "approve_listing.py":       frozenset({"STRIPE_SECRET_KEY"}),
    "sales_tracker.py":         frozenset({"STRIPE_SECRET_KEY"}),
    # Email
    "order_delivery.py":        frozenset({"STRIPE_SECRET_KEY", "EMAIL_SMTP_PASS"}),
    "email_digest.py":          frozenset({"EMAIL_SMTP_PASS"}),
    "alerts.py":                frozenset({"EMAIL_SMTP_PASS"}),
    # Reporting
    "atlas_feed.py":            frozenset({"ATLAS_FEED_TOKEN"}),
    # Local files only (the storefront deploy uses the SSH deploy key, not .env)
    "db.py":                    NONE,
    "backup_db.py":             NONE,
    "image_processor.py":       NONE,
    "listing_builder.py":       NONE,
    "pre_upload_validator.py":  NONE,
    "file_organizer.py":        NONE,
    "storefront.py":            NONE,
    "health_dashboard.py":      NONE,
}


def scoping_enabled(environ: dict[str, str], dotenv: dict[str, str | None]) -> bool:
    raw = environ.get("AGENT_ENV_SCOPING") or dotenv.get("AGENT_ENV_SCOPING") or ""
    return raw.strip().lower() in ("1", "true", "yes", "on")


def allowed_secrets(script: str) -> frozenset[str]:
    # A script that isn't listed gets no secrets until someone adds it above.
    return AGENT_SECRETS.get(Path(script).name, NONE)


def scoped_environ(script: str, environ: dict[str, str]) -> dict[str, str]:
    """The environment for `script`: every secret it doesn't need is set to ""."""
    allowed = allowed_secrets(script)
    env = dict(environ)
    for key in SECRET_KEYS - allowed:
        env[key] = ""
    return env


def main(argv: list[str]) -> None:
    if not argv:
        print("Usage: python scripts/scoped_env.py scripts/<name>.py [args...]", file=sys.stderr)
        sys.exit(2)
    script, args = argv[0], argv[1:]
    env = dict(os.environ)
    if scoping_enabled(env, dotenv_values(_ROOT / ".env")):
        if Path(script).name not in AGENT_SECRETS:
            print(f"[scoped_env] {script} is not listed in AGENT_SECRETS; running it without secrets",
                  file=sys.stderr)
        env = scoped_environ(script, env)
    os.execve(sys.executable, [sys.executable, script, *args], env)


if __name__ == "__main__":
    main(sys.argv[1:])
