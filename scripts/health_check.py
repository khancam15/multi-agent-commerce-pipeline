#!/usr/bin/env python3
"""
health_check.py — System health check and monitoring endpoint
──────────────────────────────────────────────────────────────
Validates that all critical components are working:
  • Database connectivity
  • Stripe API access
  • Canva API access
  • Anthropic API key format
  • File system permissions
  • Recent pipeline activity

Usage:
    # Command line (exit code 0 = healthy, 1 = unhealthy)
    python scripts/health_check.py

    # As a web endpoint (for monitoring tools)
    python scripts/health_check.py --serve --port 8080

    # Quick check (skip API calls, faster)
    python scripts/health_check.py --quick

    # JSON output (for parsing)
    python scripts/health_check.py --json

Exit codes:
    0 = All checks passed
    1 = One or more checks failed
    2 = Critical failure (cannot proceed)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

# Add scripts to path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

from config import settings
from db import init_db
from api_retry import rget


# ── Health Check Result ───────────────────────────────────────────────────────

@dataclass
class HealthCheckResult:
    """Result of a single health check."""
    name: str
    status: Literal["pass", "fail", "warn"]
    message: str
    latency_ms: float | None = None
    details: dict[str, Any] | None = None


@dataclass
class HealthReport:
    """Overall health report."""
    timestamp: str
    status: Literal["healthy", "degraded", "unhealthy"]
    checks: list[HealthCheckResult]
    summary: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "status": self.status,
            "checks": [asdict(check) for check in self.checks],
            "summary": self.summary,
        }


def _utc_now() -> datetime:
    """Return UTC time in the same naive ISO format used by existing rows."""
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_stored_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _ensure_runtime_dirs() -> None:
    for directory in (
        settings.outputs_dir,
        settings.logs_dir,
        settings.products_dir,
        settings.assets_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)


# ── Individual Health Checks ──────────────────────────────────────────────────

def check_database() -> HealthCheckResult:
    """Check SQLite database connectivity and integrity."""
    start = time.time()
    try:
        init_db(settings.db_path)

        with sqlite3.connect(settings.db_path, timeout=5) as conn:
            # Check tables exist
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {row[0] for row in tables}

            required = {"queue", "listings", "run_log", "seo_review", "sales"}
            missing = required - table_names

            if missing:
                return HealthCheckResult(
                    name="database",
                    status="fail",
                    message=f"Missing tables: {', '.join(missing)}",
                    latency_ms=(time.time() - start) * 1000,
                )

            # Check recent activity (listings in last 30 days)
            cutoff = (_utc_now() - timedelta(days=30)).isoformat()
            count = conn.execute(
                "SELECT COUNT(*) FROM listings WHERE published_at >= ?", (cutoff,)
            ).fetchone()[0]

            return HealthCheckResult(
                name="database",
                status="pass",
                message=f"Database healthy ({count} listings in last 30 days)",
                latency_ms=(time.time() - start) * 1000,
                details={"listings_30d": count},
            )

    except sqlite3.Error as e:
        return HealthCheckResult(
            name="database",
            status="fail",
            message=f"Database error: {str(e)[:100]}",
            latency_ms=(time.time() - start) * 1000,
        )


def check_stripe_api() -> HealthCheckResult:
    """Check Stripe API connectivity with the configured secret key."""
    start = time.time()

    if not settings.stripe_secret_key:
        return HealthCheckResult(
            name="stripe_api",
            status="fail",
            message="STRIPE_SECRET_KEY not configured",
            latency_ms=0,
        )

    try:
        resp = rget(
            "https://api.stripe.com/v1/products",
            headers={"Authorization": f"Bearer {settings.stripe_secret_key}"},
            params={"limit": 1},
            timeout=10,
            _label="Health check",
        )
        latency = (time.time() - start) * 1000

        if resp.status_code == 200:
            mode = "test" if settings.stripe_secret_key.startswith(("sk_test_", "rk_test_")) else "live"
            return HealthCheckResult(
                name="stripe_api",
                status="pass",
                message=f"Stripe API reachable ({mode} mode)",
                latency_ms=latency,
            )
        if resp.status_code in (401, 403):
            return HealthCheckResult(
                name="stripe_api",
                status="fail",
                message=f"Stripe key rejected ({resp.status_code})",
                latency_ms=latency,
            )
        return HealthCheckResult(
            name="stripe_api",
            status="warn",
            message=f"Stripe API returned {resp.status_code}",
            latency_ms=latency,
        )

    except Exception as e:
        return HealthCheckResult(
            name="stripe_api",
            status="fail",
            message=f"Stripe API error: {str(e)[:100]}",
            latency_ms=(time.time() - start) * 1000,
        )


def check_canva_api() -> HealthCheckResult:
    """Check Canva API connectivity."""
    start = time.time()

    if not settings.canva_access_token:
        return HealthCheckResult(
            name="canva_api",
            status="warn",
            message="Canva token not configured (Phase 3/3B will fail)",
            latency_ms=0,
        )

    try:
        resp = rget(
            "https://api.canva.com/rest/v1/users/me",
            headers={"Authorization": f"Bearer {settings.canva_access_token}"},
            timeout=10,
            _label="Health check",
        )

        latency = (time.time() - start) * 1000

        if resp.status_code == 200:
            team = resp.json().get("team", {})
            team_name = team.get("display_name", "Connected")
            return HealthCheckResult(
                name="canva_api",
                status="pass",
                message=f"Canva API reachable ({team_name})",
                latency_ms=latency,
            )
        elif resp.status_code == 401:
            return HealthCheckResult(
                name="canva_api",
                status="warn",
                message="Canva token expired (will auto-refresh)",
                latency_ms=latency,
            )
        elif resp.status_code == 404:
            # /users/me not available on this token scope — token is still valid
            return HealthCheckResult(
                name="canva_api",
                status="pass",
                message="Canva token present (scope check skipped)",
                latency_ms=latency,
            )
        else:
            return HealthCheckResult(
                name="canva_api",
                status="warn",
                message=f"Canva API returned {resp.status_code}",
                latency_ms=latency,
            )

    except Exception as e:
        return HealthCheckResult(
            name="canva_api",
            status="warn",
            message=f"Canva API error: {str(e)[:100]}",
            latency_ms=(time.time() - start) * 1000,
        )


def check_anthropic_key() -> HealthCheckResult:
    """Check Anthropic API key format (doesn't make a live call to save cost)."""
    if not settings.anthropic_api_key:
        return HealthCheckResult(
            name="anthropic_api",
            status="fail",
            message="Anthropic API key not configured",
            latency_ms=0,
        )

    if settings.anthropic_api_key.startswith("sk-ant-"):
        return HealthCheckResult(
            name="anthropic_api",
            status="pass",
            message="Anthropic API key format valid",
            latency_ms=0,
        )
    else:
        return HealthCheckResult(
            name="anthropic_api",
            status="warn",
            message="Anthropic API key format unexpected",
            latency_ms=0,
        )


def check_file_system() -> HealthCheckResult:
    """Check critical directories exist and are writable."""
    start = time.time()
    try:
        _ensure_runtime_dirs()

        required_dirs = [
            settings.outputs_dir,
            settings.logs_dir,
            settings.products_dir,
            settings.assets_dir,
        ]

        missing = []
        not_writable = []

        for directory in required_dirs:
            if not directory.exists():
                missing.append(str(directory.relative_to(settings.root_dir)))
            elif not directory.is_dir():
                not_writable.append(f"{directory.relative_to(settings.root_dir)} (not a directory)")
            else:
                # Test write permission
                test_file = directory / ".health_check_test"
                try:
                    test_file.touch()
                    test_file.unlink()
                except OSError:
                    not_writable.append(str(directory.relative_to(settings.root_dir)))

        if missing:
            return HealthCheckResult(
                name="file_system",
                status="fail",
                message=f"Missing directories: {', '.join(missing)}",
                latency_ms=(time.time() - start) * 1000,
            )

        if not_writable:
            return HealthCheckResult(
                name="file_system",
                status="fail",
                message=f"Not writable: {', '.join(not_writable)}",
                latency_ms=(time.time() - start) * 1000,
            )

        return HealthCheckResult(
            name="file_system",
            status="pass",
            message="All directories writable",
            latency_ms=(time.time() - start) * 1000,
        )

    except Exception as e:
        return HealthCheckResult(
            name="file_system",
            status="fail",
            message=f"File system error: {str(e)[:100]}",
            latency_ms=(time.time() - start) * 1000,
        )


def check_recent_activity() -> HealthCheckResult:
    """Check for recent pipeline activity (last publish, last run)."""
    start = time.time()
    try:
        with sqlite3.connect(settings.db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row

            # Last publish
            last_publish = conn.execute(
                "SELECT published_at FROM listings ORDER BY published_at DESC LIMIT 1"
            ).fetchone()

            # Last successful run
            last_run = conn.execute(
                "SELECT run_at, phase FROM run_log WHERE status = 'success' ORDER BY run_at DESC LIMIT 1"
            ).fetchone()

            if not last_publish and not last_run:
                return HealthCheckResult(
                    name="recent_activity",
                    status="warn",
                    message="No pipeline activity recorded yet",
                    latency_ms=(time.time() - start) * 1000,
                )

            details: dict[str, Any] = {}
            messages = []

            if last_publish:
                pub_time = _parse_stored_datetime(last_publish["published_at"])
                hours_ago = (_utc_now() - pub_time).total_seconds() / 3600
                details["last_publish_hours_ago"] = round(hours_ago, 1)
                messages.append(f"last publish {hours_ago:.0f}h ago")

            if last_run:
                run_time = _parse_stored_datetime(last_run["run_at"])
                hours_ago = (_utc_now() - run_time).total_seconds() / 3600
                details["last_run_hours_ago"] = round(hours_ago, 1)
                details["last_run_phase"] = last_run["phase"]
                messages.append(f"last run {hours_ago:.0f}h ago ({last_run['phase']})")

            return HealthCheckResult(
                name="recent_activity",
                status="pass",
                message=", ".join(messages),
                latency_ms=(time.time() - start) * 1000,
                details=details,
            )

    except Exception as e:
        return HealthCheckResult(
            name="recent_activity",
            status="warn",
            message=f"Activity check error: {str(e)[:100]}",
            latency_ms=(time.time() - start) * 1000,
        )


# ── Run All Checks ────────────────────────────────────────────────────────────

def run_health_checks(*, quick: bool = False) -> HealthReport:
    """
    Run all health checks and return a report.

    Args:
        quick: If True, skip API calls (faster, but less thorough)
    """
    checks: list[HealthCheckResult] = []

    # Always run these (fast)
    checks.append(check_anthropic_key())
    checks.append(check_file_system())
    checks.append(check_database())
    checks.append(check_recent_activity())

    # Skip API calls if quick mode
    if not quick:
        checks.append(check_stripe_api())
        checks.append(check_canva_api())

    # Determine overall status
    fail_count = sum(1 for c in checks if c.status == "fail")
    warn_count = sum(1 for c in checks if c.status == "warn")

    overall_status: Literal["healthy", "degraded", "unhealthy"]
    if fail_count > 0:
        overall_status = "unhealthy"
    elif warn_count > 0:
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    summary = {
        "total_checks": len(checks),
        "passed": sum(1 for c in checks if c.status == "pass"),
        "warnings": warn_count,
        "failures": fail_count,
    }

    return HealthReport(
        timestamp=_utc_now().isoformat(),
        status=overall_status,
        checks=checks,
        summary=summary,
    )


# ── Output Formatters ─────────────────────────────────────────────────────────

def print_report(report: HealthReport, *, json_output: bool = False) -> None:
    """Print health report to console."""
    if json_output:
        print(json.dumps(report.to_dict(), indent=2))
        return

    # Human-readable output
    status_emoji = {
        "healthy": "✅",
        "degraded": "⚠️ ",
        "unhealthy": "❌",
    }

    check_emoji = {
        "pass": "✅",
        "warn": "⚠️ ",
        "fail": "❌",
    }

    print(f"\n{status_emoji[report.status]} System Status: {report.status.upper()}")
    print(f"Timestamp: {report.timestamp}")
    print(f"\nChecks: {report.summary['passed']}/{report.summary['total_checks']} passed")

    if report.summary['warnings'] > 0:
        print(f"⚠️  Warnings: {report.summary['warnings']}")
    if report.summary['failures'] > 0:
        print(f"❌ Failures: {report.summary['failures']}")

    print("\nDetails:")
    print("-" * 60)

    for check in report.checks:
        latency = f" ({check.latency_ms:.0f}ms)" if check.latency_ms else ""
        print(f"{check_emoji[check.status]} {check.name:20s} {check.message}{latency}")

    print("-" * 60)


# ── Web Server (Optional) ─────────────────────────────────────────────────────

def serve_health_endpoint(port: int = 8080) -> None:
    """
    Start a simple HTTP server for health checks.
    Useful for monitoring tools like Kubernetes, Docker, etc.
    """
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in ["/health", "/healthz", "/"]:
                report = run_health_checks(quick=True)

                status_code = {
                    "healthy": 200,
                    "degraded": 200,  # Still accepting traffic
                    "unhealthy": 503,
                }[report.status]

                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(report.to_dict()).encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            # Suppress default logging
            pass

    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health check server running on http://0.0.0.0:{port}/health")
    print("Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Health check for the commerce pipeline")
    parser.add_argument("--quick", action="store_true", help="Skip API calls (faster)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--serve", action="store_true", help="Run as web server")
    parser.add_argument("--port", type=int, default=8080, help="Port for web server")

    args = parser.parse_args()

    if args.serve:
        serve_health_endpoint(args.port)
        return 0

    report = run_health_checks(quick=args.quick)
    print_report(report, json_output=args.json)

    # Exit code based on status
    if report.status == "unhealthy":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
