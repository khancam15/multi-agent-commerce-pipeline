"""
canva_api.py — Canva Connect API client
────────────────────────────────────────
Thin wrapper around the Canva Connect REST API v1.

Handles:
  • Token refresh (access token expires every hour)
  • Design export: POST /v1/exports → poll → return JPEG download URLs
  • Folder creation and design organisation
  • Design ID extraction from canva.com URLs

Used by canva_image_generator.py after the Anthropic MCP call returns
design URLs — this gives reliable, authenticated JPEG download links
instead of parsing unstable text from the MCP response.

Requires in .env:
  CANVA_CLIENT_ID
  CANVA_CLIENT_SECRET
  CANVA_ACCESS_TOKEN
  CANVA_REFRESH_TOKEN
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

try:
    import requests
except ImportError as _e:
    raise ImportError("requests not installed — run: .venv/bin/pip install requests") from _e

from api_retry import retry_request

# ── API base ──────────────────────────────────────────────────────────────────
_BASE       = "https://api.canva.com/rest/v1"
_TOKEN_URL  = f"{_BASE}/oauth/token"
_EXPORT_URL = f"{_BASE}/exports"
_FOLDER_URL = f"{_BASE}/folders"

# Poll settings
_POLL_INTERVAL = 3   # seconds between status checks
_POLL_TIMEOUT  = 120  # give up after 2 minutes per export


class CanvaAPIError(RuntimeError):
    pass


class CanvaClient:
    """
    Stateless Canva Connect API client.

    Token refresh is triggered automatically on 401 responses.
    Refreshed tokens are written back to .env so the rest of the
    pipeline picks them up without re-running canva_oauth.py.
    """

    def __init__(self) -> None:
        self._client_id     = os.getenv("CANVA_CLIENT_ID", "")
        self._client_secret = os.getenv("CANVA_CLIENT_SECRET", "")
        self._access_token  = os.getenv("CANVA_ACCESS_TOKEN", "")
        self._refresh_token = os.getenv("CANVA_REFRESH_TOKEN", "")

        if not self._access_token:
            raise CanvaAPIError(
                "CANVA_ACCESS_TOKEN not set in .env — run: python scripts/canva_oauth.py"
            )
        if not self._refresh_token:
            raise CanvaAPIError(
                "CANVA_REFRESH_TOKEN not set in .env — run: python scripts/canva_oauth.py"
            )

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type":  "application/json",
        }

    def _refresh_access_token(self) -> None:
        """Exchange refresh token for a new access token and persist to .env."""
        print("  [canva_api] Access token expired — refreshing...")

        if not self._client_id or not self._client_secret:
            raise CanvaAPIError(
                "CANVA_CLIENT_ID / CANVA_CLIENT_SECRET not set — cannot refresh token.\n"
                "Re-run: python scripts/canva_oauth.py"
            )

        resp = retry_request(
            "POST",
            _TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "client_id":     self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            },
            timeout=30,
            _label="Canva token refresh",
        )

        if not resp.ok:
            raise CanvaAPIError(
                f"Token refresh failed ({resp.status_code}): {resp.text[:300]}"
            )

        data = resp.json()
        self._access_token  = data.get("access_token", self._access_token)
        self._refresh_token = data.get("refresh_token", self._refresh_token)

        # Persist to .env so other scripts pick up the new token
        self._update_env({
            "CANVA_ACCESS_TOKEN":  self._access_token,
            "CANVA_REFRESH_TOKEN": self._refresh_token,
        })
        print("  [canva_api] Token refreshed and saved to .env")

    @staticmethod
    def _update_env(updates: dict[str, str]) -> None:
        env_path = _ROOT / ".env"
        existing: dict[str, str] = {}
        if env_path.exists():
            existing = {k: v for k, v in dotenv_values(env_path).items() if v is not None}
        existing.update(updates)
        lines = [f"{k}={v}\n" for k, v in existing.items()]
        env_path.write_text("".join(lines), encoding="utf-8")

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        Make an authenticated request with retry + backoff on transient errors.
        Refreshes the access token once on 401 and retries.
        """
        resp = retry_request(method, url, headers=self._headers(), **kwargs)
        if resp.status_code == 401:
            self._refresh_access_token()
            resp = retry_request(method, url, headers=self._headers(), **kwargs)
        return resp

    # ── Design ID extraction ──────────────────────────────────────────────────

    @staticmethod
    def design_id_from_url(url: str) -> str:
        """
        Extract design ID from a Canva URL.
        Handles:
          https://www.canva.com/design/DAF.../view
          https://www.canva.com/design/DAF...
        """
        if not url:
            raise CanvaAPIError("Empty URL passed to design_id_from_url")
        try:
            after_design = url.split("/design/")[1]
            return after_design.split("/")[0].split("?")[0]
        except (IndexError, AttributeError) as exc:
            raise CanvaAPIError(f"Cannot extract design ID from URL: {url!r}") from exc

    # ── Export ────────────────────────────────────────────────────────────────

    def export_design(
        self,
        design_id: str,
        fmt: str = "jpg",
        quality: int = 92,
    ) -> list[str]:
        """
        Export a Canva design as JPEG (or other format) via the Connect API.

        Steps:
          1. POST /v1/exports to create an export job
          2. Poll GET /v1/exports/{job_id} until status == "success"
          3. Return list of download URLs (one per page)

        Args:
            design_id: Canva design ID (e.g. "DAF1234abCD5")
            fmt:       Export format — "jpg", "png", or "pdf"
            quality:   JPEG quality 1–100 (ignored for png/pdf)

        Returns:
            List of HTTPS download URLs for the exported pages.

        Raises:
            CanvaAPIError on failure or timeout.
        """
        # Build format payload
        if fmt == "jpg":
            format_payload: dict = {"type": "jpg", "quality": quality}
        elif fmt == "png":
            format_payload = {"type": "png", "lossless": False}
        elif fmt == "pdf":
            format_payload = {"type": "pdf", "export_quality": "regular"}
        else:
            format_payload = {"type": fmt}

        payload = {
            "design_id": design_id,
            "format":    format_payload,
        }

        # A duplicate export job is harmless, so allow full retries
        resp = self._request("POST", _EXPORT_URL, json=payload, timeout=30, _idempotent=True)
        if not resp.ok:
            raise CanvaAPIError(
                f"Export request failed for {design_id} ({resp.status_code}): {resp.text[:300]}"
            )

        job = resp.json()
        job_id = job.get("job", {}).get("id") or job.get("id")
        if not job_id:
            raise CanvaAPIError(
                f"No job ID in export response for {design_id}: {job}"
            )

        # Poll until done
        poll_url = f"{_EXPORT_URL}/{job_id}"
        deadline = time.time() + _POLL_TIMEOUT
        while time.time() < deadline:
            time.sleep(_POLL_INTERVAL)
            poll_resp = retry_request("GET", poll_url, headers=self._headers(), timeout=30)
            if not poll_resp.ok:
                raise CanvaAPIError(
                    f"Export poll failed ({poll_resp.status_code}): {poll_resp.text[:300]}"
                )

            data   = poll_resp.json()
            status = (
                data.get("job", {}).get("status")
                or data.get("status")
                or "unknown"
            )

            if status == "success":
                urls = (
                    data.get("job", {}).get("urls")
                    or data.get("urls")
                    or []
                )
                if not urls:
                    raise CanvaAPIError(
                        f"Export job {job_id} succeeded but returned no URLs: {data}"
                    )
                return urls  # type: ignore[return-value]

            if status == "failed":
                error_msg = (
                    data.get("job", {}).get("error")
                    or data.get("error")
                    or str(data)
                )
                raise CanvaAPIError(
                    f"Export job {job_id} failed for design {design_id}: {error_msg}"
                )

            # still "in_progress" — keep polling

        raise CanvaAPIError(
            f"Export job {job_id} timed out after {_POLL_TIMEOUT}s for design {design_id}"
        )

    # ── Folder management ─────────────────────────────────────────────────────

    def create_folder(self, name: str, parent_folder_id: str = "root") -> str:
        """
        Create a Canva folder and return its folder_id.

        Args:
            name:             Display name for the folder
            parent_folder_id: Parent folder ID (default: "root")

        Returns:
            folder_id string
        """
        resp = self._request(
            "POST",
            _FOLDER_URL,
            json={"name": name, "parent_folder_id": parent_folder_id},
            timeout=30,
        )
        if not resp.ok:
            raise CanvaAPIError(
                f"Create folder '{name}' failed ({resp.status_code}): {resp.text[:300]}"
            )

        data      = resp.json()
        folder_id = (
            data.get("folder", {}).get("id")
            or data.get("id")
        )
        if not folder_id:
            raise CanvaAPIError(f"No folder ID in response: {data}")
        return folder_id

    def move_to_folder(self, folder_id: str, item_id: str) -> None:
        """
        Move a design (or asset) into a folder.

        Args:
            folder_id: Destination folder ID
            item_id:   Design or asset ID to move
        """
        url  = f"{_FOLDER_URL}/{folder_id}/items/move"
        resp = self._request(
            "POST",
            url,
            json={"item_id": item_id},
            timeout=30,
        )
        if not resp.ok:
            # Non-fatal — log a warning but don't raise
            print(
                f"  [canva_api] WARNING: move_to_folder failed "
                f"({resp.status_code}): {resp.text[:200]}"
            )

    # ── Template sharing ──────────────────────────────────────────────────────

    def get_template_link(self, design_id: str) -> str:
        """
        Create a "Use template" shareable link for a Canva design.

        When a buyer clicks this link, Canva creates their own editable
        copy — they never touch the original. This is the standard
        delivery mechanism for Canva templates sold through the store.

        Returns a canva.com URL string.
        Raises CanvaAPIError if no template link could be created. There is
        deliberately no fallback: a preview or edit URL of the original
        design is not something a buyer can use, and may expose the original.
        """
        url  = f"{_BASE}/designs/{design_id}/links"
        resp = self._request(
            "POST",
            url,
            json={"action": "use_template"},
            timeout=30,
            _idempotent=True,  # an extra share link is harmless
        )

        if resp.ok:
            data     = resp.json()
            link_url = (
                data.get("link", {}).get("url")
                or data.get("url")
                or ""
            )
            if link_url:
                return link_url

        raise CanvaAPIError(
            f"Could not create a template link for design {design_id} "
            f"({resp.status_code}): {resp.text[:200]}"
        )

    # ── Convenience: export by URL ────────────────────────────────────────────

    def export_from_url(
        self,
        canva_url: str,
        fmt: str = "jpg",
        quality: int = 92,
    ) -> list[str]:
        """
        Extract design ID from a canva.com URL then export it.

        Returns list of download URLs.
        """
        design_id = self.design_id_from_url(canva_url)
        return self.export_design(design_id, fmt=fmt, quality=quality)
