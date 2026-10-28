"""
config.py — Centralized configuration management
─────────────────────────────────────────────────
Single source of truth for all configuration values.

Replaces scattered hardcoded values with typed, validated settings.

Usage:
    from config import settings

    print(settings.shopify_store_domain)
    print(settings.weekly_publish_limit)
    print(settings.max_retries)

Features:
  • Type-safe configuration (Pydantic)
  • Automatic .env loading
  • Validation on startup
  • Default values
  • Environment variable override

Environment variables:
    All settings can be overridden via environment variables.
    Example: WEEKLY_PUBLISH_LIMIT=10 overrides the default.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

# Load .env before defining settings
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


# ── Settings Class ────────────────────────────────────────────────────────────

class Settings:
    """
    Centralized application settings.

    Note: Using a simple class instead of pydantic-settings to avoid
    adding a heavy dependency. For production, consider switching to
    pydantic-settings for better validation.
    """

    def __init__(self):
        # ── API Keys ──────────────────────────────────────────────────────────
        self.anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
        self.serper_api_key: str = os.getenv("SERPER_API_KEY", "")

        # ── Shopify ───────────────────────────────────────────────────────────
        self.shopify_store_domain: str = os.getenv("SHOPIFY_STORE_DOMAIN", "")
        self.shopify_access_token: str = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
        self.shopify_api_version: str = os.getenv("SHOPIFY_API_VERSION", "2026-07")
        self.shopify_vendor: str = os.getenv("SHOPIFY_VENDOR", os.getenv("BRAND_NAME", "Your Brand"))

        # ── Canva ─────────────────────────────────────────────────────────────
        self.canva_client_id: str = os.getenv("CANVA_CLIENT_ID", "")
        self.canva_client_secret: str = os.getenv("CANVA_CLIENT_SECRET", "")
        self.canva_access_token: str = os.getenv("CANVA_ACCESS_TOKEN", "")
        self.canva_refresh_token: str = os.getenv("CANVA_REFRESH_TOKEN", "")
        self.canva_mcp_token: str = os.getenv("CANVA_MCP_TOKEN", "")

        # ── Publishing Cadence ────────────────────────────────────────────────
        self.weekly_publish_limit: int = int(os.getenv("WEEKLY_PUBLISH_LIMIT", "5"))
        self.publish_cooldown_hours: int = int(os.getenv("PUBLISH_COOLDOWN_HOURS", "24"))

        # ── API Retry Configuration ───────────────────────────────────────────
        self.max_retries: int = int(os.getenv("MAX_RETRIES", "3"))
        self.backoff_base: int = int(os.getenv("BACKOFF_BASE", "2"))
        self.max_backoff: int = int(os.getenv("MAX_BACKOFF", "60"))

        # ── Circuit Breaker ───────────────────────────────────────────────────
        self.circuit_failure_threshold: int = int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", "5"))
        self.circuit_timeout_seconds: int = int(os.getenv("CIRCUIT_TIMEOUT_SECONDS", "300"))

        # ── Email ─────────────────────────────────────────────────────────────
        self.email_to: str = os.getenv("EMAIL_TO", "")
        self.email_from: str = os.getenv("EMAIL_FROM", "")
        self.email_smtp_pass: str = os.getenv("EMAIL_SMTP_PASS", "")

        # ── Logging ───────────────────────────────────────────────────────────
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
        self.log_format: Literal["console", "json"] = os.getenv("LOG_FORMAT", "console")  # type: ignore

        # ── CrewAI ────────────────────────────────────────────────────────────
        self.crewai_tracing_enabled: bool = os.getenv("CREWAI_TRACING_ENABLED", "false").lower() == "true"

        # ── Paths ─────────────────────────────────────────────────────────────
        self.root_dir: Path = _ROOT
        self.outputs_dir: Path = _ROOT / "outputs"
        self.products_dir: Path = _ROOT / "02_Products"
        self.assets_dir: Path = _ROOT / "04_Assets" / "ReadyToUpload"
        self.logs_dir: Path = _ROOT / "logs"
        self.db_path: Path = self.outputs_dir / "pipeline.db"

        # Ensure directories exist
        self.outputs_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)

    def validate(self) -> list[str]:
        """
        Validate required settings are present.

        Returns:
            List of missing or invalid settings (empty if all valid)
        """
        errors = []

        # Required API keys
        if not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY is required")
        if not self.shopify_store_domain:
            errors.append("SHOPIFY_STORE_DOMAIN is required")
        if not self.shopify_access_token:
            errors.append("SHOPIFY_ACCESS_TOKEN is required")

        # Validate numeric ranges
        if not (1 <= self.weekly_publish_limit <= 100):
            errors.append("WEEKLY_PUBLISH_LIMIT must be 1-100")
        if not (1 <= self.publish_cooldown_hours <= 168):
            errors.append("PUBLISH_COOLDOWN_HOURS must be 1-168 (1 week)")
        if not (1 <= self.max_retries <= 10):
            errors.append("MAX_RETRIES must be 1-10")

        return errors

    def is_valid(self) -> bool:
        """Check if all required settings are valid."""
        return len(self.validate()) == 0

    def print_summary(self) -> None:
        """Print a summary of current settings (masking sensitive values)."""
        def mask(value: str) -> str:
            if not value:
                return "<not set>"
            if len(value) <= 8:
                return "***"
            return value[:4] + "***" + value[-4:]

        print("=" * 60)
        print("Configuration Summary")
        print("=" * 60)
        print(f"Shopify Store:        {self.shopify_store_domain}")
        print(f"Shopify API Version:  {self.shopify_api_version}")
        print(f"Shopify Vendor:       {self.shopify_vendor}")
        print(f"Weekly Publish Limit: {self.weekly_publish_limit}")
        print(f"Publish Cooldown:     {self.publish_cooldown_hours}h")
        print(f"Max Retries:          {self.max_retries}")
        print(f"Circuit Threshold:    {self.circuit_failure_threshold} failures")
        print(f"Log Level:            {self.log_level}")
        print(f"Log Format:           {self.log_format}")
        print()
        print("API Keys:")
        print(f"  Anthropic:          {mask(self.anthropic_api_key)}")
        print(f"  Serper:             {mask(self.serper_api_key)}")
        print(f"  Shopify Token:      {mask(self.shopify_access_token)}")
        print(f"  Canva Access:       {mask(self.canva_access_token)}")
        print("=" * 60)

        # Print validation errors if any
        errors = self.validate()
        if errors:
            print("\n⚠️  Configuration Errors:")
            for error in errors:
                print(f"  • {error}")
            print()


# ── Global Settings Instance ──────────────────────────────────────────────────

settings = Settings()


# ── Backward Compatibility Helpers ───────────────────────────────────────────

def get_env(key: str, default: str = "") -> str:
    """
    Backward compatibility helper.
    Gradually replace os.getenv() calls with settings.attribute_name.
    """
    return os.getenv(key, default)


# ── CLI Test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    settings.print_summary()

    errors = settings.validate()
    if errors:
        print("\n❌ Configuration is INVALID")
        sys.exit(1)
    else:
        print("\n✅ Configuration is valid")
        sys.exit(0)
