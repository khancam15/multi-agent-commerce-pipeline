"""
api_retry.py — Shared HTTP retry utility
─────────────────────────────────────────
Drop-in replacement for requests.get / requests.post / requests.request
with exponential backoff on transient failures.

Retryable conditions:
  • 429 Too Many Requests  — respects Retry-After header if present
  • 500 / 502 / 503 / 504  — server-side transient errors
  • ConnectionError        — network blip
  • Timeout               — server took too long

Non-retryable (raised immediately):
  • 4xx other than 429     — bad request, auth failure, not found
  • RequestException       — malformed request

Non-idempotent methods (POST, PATCH) are only retried on 429, which means
the server rejected the request without processing it. A timeout, dropped
connection or 5xx on a POST may mean the server already acted (for example
created a Shopify product), so retrying could duplicate it. Pass
_idempotent=True to opt a POST into full retries when duplicates are harmless.

Usage:
    from api_retry import retry_request

    # Same signature as requests.request()
    resp = retry_request("GET", url, headers=hdrs, timeout=30)
    resp = retry_request("POST", url, json=body, timeout=60)

    # Convenience wrappers
    from api_retry import rget, rpost, rput
    resp = rget(url, headers=hdrs, timeout=30)
    resp = rpost(url, json=body, timeout=60)
    resp = rput(url, json=body, timeout=30)
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import requests

# ── Config ────────────────────────────────────────────────────────────────────
# Overridable from .env (same names as config.py); callers load .env before importing
MAX_RETRIES      = int(os.getenv("MAX_RETRIES", "3"))
BACKOFF_BASE     = int(os.getenv("BACKOFF_BASE", "2"))   # seconds — delays are 2s, 4s, 8s
MAX_BACKOFF      = int(os.getenv("MAX_BACKOFF", "60"))   # cap so we never wait longer than a minute
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}

# Circuit breaker config. State is kept in memory, so it only protects calls
# made within one process (one phase run); each phase starts with it closed.
CIRCUIT_FAILURE_THRESHOLD = int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", "5"))
CIRCUIT_TIMEOUT_SECONDS   = int(os.getenv("CIRCUIT_TIMEOUT_SECONDS", "300"))
CIRCUIT_HALF_OPEN_ATTEMPTS = 1     # Allow 1 test request when half-open

# ── Circuit Breaker State ─────────────────────────────────────────────────────

class CircuitState:
    """Tracks circuit breaker state per API host."""
    def __init__(self):
        self.failure_count = 0
        self.last_failure_time: datetime | None = None
        self.state = "closed"  # closed, open, half_open

_circuit_state: dict[str, CircuitState] = {}


def _get_host(url: str) -> str:
    """Extract host from URL for circuit breaker tracking."""
    return urlparse(url).netloc


def _check_circuit(host: str) -> tuple[bool, str]:
    """
    Check if circuit breaker allows requests to this host.

    Returns:
        (allowed, reason)
        - allowed: True if request should proceed, False if blocked
        - reason: Human-readable explanation
    """
    if host not in _circuit_state:
        _circuit_state[host] = CircuitState()

    circuit = _circuit_state[host]
    now = datetime.now()

    # Circuit is closed (normal operation)
    if circuit.state == "closed":
        return True, "Circuit closed"

    # Circuit is open (blocking requests)
    if circuit.state == "open":
        # Check if timeout has elapsed
        if circuit.last_failure_time and now - circuit.last_failure_time > timedelta(seconds=CIRCUIT_TIMEOUT_SECONDS):
            circuit.state = "half_open"
            return True, "Circuit half-open (testing)"

        time_left = CIRCUIT_TIMEOUT_SECONDS
        if circuit.last_failure_time:
            elapsed = (now - circuit.last_failure_time).total_seconds()
            time_left = int(CIRCUIT_TIMEOUT_SECONDS - elapsed)

        return False, f"Circuit open (retry in {time_left}s)"

    # Circuit is half-open (testing if service recovered)
    if circuit.state == "half_open":
        return True, "Circuit half-open (test request)"

    return True, "Unknown state"


def _record_success(host: str) -> None:
    """Record successful request, reset circuit breaker."""
    if host in _circuit_state:
        circuit = _circuit_state[host]
        circuit.failure_count = 0
        circuit.state = "closed"


def _record_failure(host: str) -> None:
    """Record failed request, potentially open circuit breaker."""
    if host not in _circuit_state:
        _circuit_state[host] = CircuitState()

    circuit = _circuit_state[host]
    circuit.failure_count += 1
    circuit.last_failure_time = datetime.now()

    # Open circuit if threshold exceeded
    if circuit.failure_count >= CIRCUIT_FAILURE_THRESHOLD:
        if circuit.state != "open":
            print(f"  ⚠️  Circuit breaker OPENED for {host} (failures: {circuit.failure_count})")
        circuit.state = "open"


def retry_request(
    method: str,
    url: str,
    *,
    _label: str = "",          # optional human-readable label for log lines
    _idempotent: bool | None = None,  # None = decide from the HTTP method
    **kwargs: Any,
) -> requests.Response:
    """
    Make an HTTP request with automatic retry, exponential backoff, and circuit breaker.

    Args:
        method:  HTTP method string — "GET", "POST", "PUT", etc.
        url:     Full URL.
        _label:  Optional label shown in retry log lines (e.g. "Shopify create listing").
        **kwargs: Passed directly to requests.request() — headers, json, data, timeout, etc.

    Returns:
        requests.Response on any non-retryable response (including final-attempt failures).

    Raises:
        requests.ConnectionError / requests.Timeout after all retries exhausted.
        RuntimeError if circuit breaker is open (service is down).
    """
    label = f"[{_label}] " if _label else ""
    host = _get_host(url)
    idempotent = (
        method.upper() in IDEMPOTENT_METHODS if _idempotent is None else _idempotent
    )
    last_exc: Exception | None = None

    # Check circuit breaker before attempting request
    allowed, reason = _check_circuit(host)
    if not allowed:
        raise RuntimeError(f"{label}Circuit breaker open for {host}: {reason}")

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, **kwargs)

            if resp.status_code not in RETRYABLE_STATUS:
                # Success or non-retryable error - reset circuit breaker
                if resp.ok:
                    _record_success(host)
                return resp  # success or a non-retryable error (4xx)

            # Retryable HTTP status - record as failure
            _record_failure(host)

            # A 5xx on a non-idempotent request may have been processed — don't repeat it
            if attempt == MAX_RETRIES or (resp.status_code != 429 and not idempotent):
                # Exhausted — return the failure response so callers can inspect it
                return resp

            # Honour Retry-After header (Shopify and Canva both send it on 429)
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = min(int(retry_after), MAX_BACKOFF)
                except ValueError:
                    wait = min(BACKOFF_BASE ** (attempt + 1), MAX_BACKOFF)
            else:
                wait = min(BACKOFF_BASE ** (attempt + 1), MAX_BACKOFF)

            print(
                f"  {label}HTTP {resp.status_code} — "
                f"retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})"
            )
            time.sleep(wait)

        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            _record_failure(host)  # Record network failures too

            # The request may have reached the server — only repeat it if that is safe
            if attempt == MAX_RETRIES or not idempotent:
                raise

            wait = min(BACKOFF_BASE ** (attempt + 1), MAX_BACKOFF)
            print(
                f"  {label}Connection error — "
                f"retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES}): {exc}"
            )
            time.sleep(wait)

    # Should never reach here, but satisfy the type checker
    if last_exc:
        raise last_exc
    raise requests.ConnectionError("retry_request: exhausted without response")


# ── Convenience wrappers ──────────────────────────────────────────────────────

def rget(url: str, **kwargs: Any) -> requests.Response:
    return retry_request("GET", url, **kwargs)


def rpost(url: str, **kwargs: Any) -> requests.Response:
    return retry_request("POST", url, **kwargs)


def rput(url: str, **kwargs: Any) -> requests.Response:
    return retry_request("PUT", url, **kwargs)
