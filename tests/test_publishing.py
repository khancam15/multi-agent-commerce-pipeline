import json
import sys
import types
from pathlib import Path

import pytest
import requests

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import api_retry
import shopify_uploader
from listing_builder import strip_template_links


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {}
        self.text = json.dumps(self._payload)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload


# ── HTML conversion ───────────────────────────────────────────────────────────

def test_to_html_escapes_model_text() -> None:
    html = shopify_uploader._to_html('<script>alert(1)</script>\n- <b>bold</b> & more')

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<li>&lt;b&gt;bold&lt;/b&gt; &amp; more</li>" in html


def test_to_html_closes_numbered_lists_with_ol() -> None:
    html = shopify_uploader._to_html("1. one\n2. two\n\n- a")

    assert html.count("<ol>") == 1 and html.count("</ol>") == 1
    assert html.index("</ol>") < html.index("<ul>")
    assert html.endswith("</ul>")


def test_strip_template_links_removes_old_delivery_block() -> None:
    description = (
        "Great kit.\n\n──────\n✦ HOW TO ACCESS YOUR TEMPLATE\n──────\n"
        "After purchase:\nhttps://www.canva.com/design/abc/view\n"
    )

    assert strip_template_links(description) == "Great kit."
    assert "canva.com" not in strip_template_links("see https://canva.com/design/X now")


# ── Retry behaviour ───────────────────────────────────────────────────────────

@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(api_retry.time, "sleep", lambda _s: None)
    api_retry._circuit_state.clear()


def test_post_is_not_retried_after_timeout(monkeypatch, no_sleep) -> None:
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(method)
        raise requests.Timeout("slow")

    monkeypatch.setattr(api_retry.requests, "request", fake_request)

    with pytest.raises(requests.Timeout):
        api_retry.retry_request("POST", "https://shop.test/products.json")
    assert calls == ["POST"]


def test_post_is_not_retried_after_server_error(monkeypatch, no_sleep) -> None:
    calls = []
    monkeypatch.setattr(
        api_retry.requests, "request",
        lambda method, url, **kw: calls.append(method) or FakeResponse(502),
    )

    resp = api_retry.retry_request("POST", "https://shop.test/products.json")

    assert resp.status_code == 502
    assert len(calls) == 1


def test_post_is_retried_on_rate_limit(monkeypatch, no_sleep) -> None:
    responses = [FakeResponse(429), FakeResponse(201)]
    monkeypatch.setattr(api_retry.requests, "request", lambda *a, **kw: responses.pop(0))

    resp = api_retry.retry_request("POST", "https://shop.test/products.json")

    assert resp.status_code == 201


def test_get_is_retried_after_timeout(monkeypatch, no_sleep) -> None:
    outcomes = [requests.Timeout("slow"), FakeResponse(200)]

    def fake_request(*a, **kw):
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(api_retry.requests, "request", fake_request)

    assert api_retry.retry_request("GET", "https://shop.test/x").status_code == 200


# ── Upload flow ───────────────────────────────────────────────────────────────

@pytest.fixture
def staged_product(tmp_path, monkeypatch):
    """A product staged for upload, with DB writes and Shopify calls captured."""
    name = "Media Kit"
    assets = tmp_path / "04_Assets" / "ReadyToUpload"
    (assets / name).mkdir(parents=True)
    mockups = tmp_path / "02_Products" / name / "Mockups"
    mockups.mkdir(parents=True)
    (mockups / "01.jpg").write_bytes(b"jpeg")

    payload = {
        "queue_id": 7, "product_name": name, "title": "Creator Media Kit",
        "tags": ["media kit"], "description": "A clean media kit.", "price": 12.0,
        "category": "Digital Template", "template_link": "https://canva.link/abc123",
    }
    (assets / name / "listing.json").write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(shopify_uploader, "_ROOT", tmp_path)
    monkeypatch.setattr(shopify_uploader, "ASSETS_DIR", assets)
    monkeypatch.setattr(shopify_uploader, "_load_env", lambda: {
        "SHOPIFY_STORE_DOMAIN": "shop.test", "SHOPIFY_ACCESS_TOKEN": "shpat_x",
    })
    monkeypatch.delenv("SHOPIFY_PUBLISH_STATUS", raising=False)
    monkeypatch.delenv("ALLOW_PUBLISH_WITHOUT_TEMPLATE", raising=False)
    monkeypatch.setitem(sys.modules, "file_organizer", types.SimpleNamespace(archive=lambda n: None))

    captured = {"listings": [], "queue": [], "requests": []}
    monkeypatch.setattr(shopify_uploader, "log_run", lambda *a, **kw: None)
    monkeypatch.setattr(shopify_uploader, "insert_listing",
                        lambda *a, **kw: captured["listings"].append((a, kw)))
    monkeypatch.setattr(shopify_uploader, "update_queue_status",
                        lambda qid, status: captured["queue"].append((qid, status)))
    return name, payload, assets, captured


def _fake_shopify(captured, existing=None):
    def fake(method, url, **kwargs):
        captured["requests"].append((method, url, kwargs))
        if method == "GET":
            return FakeResponse(200, {"products": [existing] if existing else []})
        product = kwargs["json"]["product"]
        return FakeResponse(201, {"product": {
            "id": 555, "handle": "creator-media-kit", "status": product["status"],
        }})
    return fake


def test_upload_creates_draft_without_template_link_in_description(staged_product, monkeypatch) -> None:
    name, payload, _assets, captured = staged_product
    monkeypatch.setattr(shopify_uploader, "retry_request", _fake_shopify(captured))

    url = shopify_uploader.upload_listing(name)

    post = [r for r in captured["requests"] if r[0] == "POST"]
    assert len(post) == 1
    product = post[0][2]["json"]["product"]
    assert product["status"] == "draft"
    assert payload["template_link"] not in product["body_html"]
    assert "email you a link" in product["body_html"]
    assert url == "https://shop.test/products/creator-media-kit"
    (_args, kwargs), = captured["listings"]
    assert kwargs["template_link"] == payload["template_link"]
    assert kwargs["status"] == "draft"
    assert captured["queue"] == [(7, "published")]


def test_upload_reuses_existing_product_instead_of_duplicating(staged_product, monkeypatch) -> None:
    name, _payload, _assets, captured = staged_product
    existing = {"id": 999, "handle": "creator-media-kit", "title": "Creator Media Kit", "status": "draft"}
    monkeypatch.setattr(shopify_uploader, "retry_request", _fake_shopify(captured, existing))

    shopify_uploader.upload_listing(name)

    assert [r[0] for r in captured["requests"]] == ["GET"]
    (_args, kwargs), = captured["listings"]
    assert kwargs["shopify_product_id"] == 999


def test_upload_refuses_without_template_link(staged_product, monkeypatch) -> None:
    name, payload, assets, captured = staged_product
    payload["template_link"] = ""
    (assets / name / "listing.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(shopify_uploader, "retry_request", _fake_shopify(captured))

    with pytest.raises(RuntimeError, match="No template link"):
        shopify_uploader.upload_listing(name)
    assert captured["requests"] == []
