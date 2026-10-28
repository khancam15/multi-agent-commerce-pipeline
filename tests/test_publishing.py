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
import db
import sales_tracker
import storefront
import stripe_common
import stripe_publisher
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
    html = storefront.to_html('<script>alert(1)</script>\n- <b>bold</b> & more')

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<li>&lt;b&gt;bold&lt;/b&gt; &amp; more</li>" in html


def test_to_html_closes_numbered_lists_with_ol() -> None:
    html = storefront.to_html("1. one\n2. two\n\n- a")

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


def test_slugify_is_url_safe() -> None:
    assert storefront.slugify("Café Menu | Editable — Canva!") == "cafe-menu-editable-canva"
    assert storefront.slugify("***") == "product"


# ── Stripe request helpers ────────────────────────────────────────────────────

def test_form_encode_uses_stripe_brackets() -> None:
    pairs = stripe_common.form_encode({
        "name": "Kit",
        "skip": None,
        "images": ["a.jpg", "b.jpg"],
        "metadata": {"queue_id": "7"},
        "line_items": [{"price": "price_1", "quantity": 1}],
        "active": True,
    })

    assert pairs == [
        ("name", "Kit"),
        ("images[0]", "a.jpg"),
        ("images[1]", "b.jpg"),
        ("metadata[queue_id]", "7"),
        ("line_items[0][price]", "price_1"),
        ("line_items[0][quantity]", "1"),
        ("active", "true"),
    ]


def test_write_sends_idempotency_key_and_is_retry_safe(monkeypatch) -> None:
    calls = []
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(stripe_common, "retry_request",
                        lambda method, url, **kw: calls.append((method, url, kw)) or FakeResponse(200, {"id": "prod_1"}))

    assert stripe_common.request("POST", "/products", data={"name": "Kit"}, idempotency_key="k1") == {"id": "prod_1"}

    (_method, url, kwargs), = calls
    assert url == "https://api.stripe.com/v1/products"
    assert kwargs["headers"]["Idempotency-Key"] == "k1"
    assert kwargs["headers"]["Authorization"] == "Bearer sk_test_x"
    assert kwargs["_idempotent"] is True


def test_stripe_error_carries_message(monkeypatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(stripe_common, "retry_request", lambda *a, **kw: FakeResponse(
        400, {"error": {"message": "Invalid currency"}}))

    with pytest.raises(stripe_common.StripeError, match="Invalid currency"):
        stripe_common.request("POST", "/products", data={"name": "Kit"})


def test_request_requires_secret_key(monkeypatch) -> None:
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)

    with pytest.raises(stripe_common.StripeError, match="STRIPE_SECRET_KEY"):
        stripe_common.request("GET", "/products")


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
        api_retry.retry_request("POST", "https://api.test/products")
    assert calls == ["POST"]


def test_idempotent_post_is_retried_after_timeout(monkeypatch, no_sleep) -> None:
    outcomes = [requests.Timeout("slow"), FakeResponse(200)]

    def fake_request(*a, **kw):
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(api_retry.requests, "request", fake_request)

    resp = api_retry.retry_request("POST", "https://api.test/products", _idempotent=True)
    assert resp.status_code == 200


def test_post_is_not_retried_after_server_error(monkeypatch, no_sleep) -> None:
    calls = []
    monkeypatch.setattr(
        api_retry.requests, "request",
        lambda method, url, **kw: calls.append(method) or FakeResponse(502),
    )

    resp = api_retry.retry_request("POST", "https://api.test/products")

    assert resp.status_code == 502
    assert len(calls) == 1


def test_post_is_retried_on_rate_limit(monkeypatch, no_sleep) -> None:
    responses = [FakeResponse(429), FakeResponse(201)]
    monkeypatch.setattr(api_retry.requests, "request", lambda *a, **kw: responses.pop(0))

    resp = api_retry.retry_request("POST", "https://api.test/products")

    assert resp.status_code == 201


def test_get_is_retried_after_timeout(monkeypatch, no_sleep) -> None:
    outcomes = [requests.Timeout("slow"), FakeResponse(200)]

    def fake_request(*a, **kw):
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(api_retry.requests, "request", fake_request)

    assert api_retry.retry_request("GET", "https://api.test/x").status_code == 200


# ── Publish flow ──────────────────────────────────────────────────────────────

@pytest.fixture
def staged_product(tmp_path, monkeypatch):
    """A product staged for publishing, with DB writes and Stripe calls captured."""
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

    monkeypatch.setattr(stripe_publisher, "_ROOT", tmp_path)
    monkeypatch.setattr(stripe_publisher, "ASSETS_DIR", assets)
    monkeypatch.setenv("STOREFRONT_BASE_URL", "https://shop.test")
    monkeypatch.setenv("STORE_CURRENCY", "usd")
    monkeypatch.delenv("STORE_PUBLISH_STATUS", raising=False)
    monkeypatch.delenv("ALLOW_PUBLISH_WITHOUT_TEMPLATE", raising=False)
    monkeypatch.setitem(sys.modules, "file_organizer", types.SimpleNamespace(archive=lambda n: None))
    monkeypatch.setattr(stripe_publisher, "get_listing", lambda n: None)
    monkeypatch.setattr(stripe_publisher, "unique_slug", lambda t: "creator-media-kit")
    monkeypatch.setattr(stripe_publisher, "save_web_images",
                        lambda slug, paths: [f"p/{slug}/{i}.jpg" for i, _ in enumerate(paths, 1)])

    captured = {"listings": [], "queue": [], "requests": []}
    monkeypatch.setattr(stripe_publisher, "log_run", lambda *a, **kw: None)
    monkeypatch.setattr(stripe_publisher, "insert_listing",
                        lambda *a, **kw: captured["listings"].append((a, kw)))
    monkeypatch.setattr(stripe_publisher, "update_queue_status",
                        lambda qid, status: captured["queue"].append((qid, status)))
    return name, payload, assets, captured


def _fake_stripe(captured, existing=None):
    def fake(method, path, **kwargs):
        captured["requests"].append((method, path, kwargs))
        if path == "/products/search":
            return {"data": [existing] if existing else []}
        if path == "/products":
            return {"id": "prod_555", "default_price": "price_555"}
        if path == "/payment_links":
            return {"id": "plink_1", "url": "https://buy.stripe.com/test_1"}
        raise AssertionError(path)
    return fake


def test_publish_creates_draft_product_without_payment_link(staged_product, monkeypatch) -> None:
    name, payload, _assets, captured = staged_product
    monkeypatch.setattr(stripe_publisher, "request", _fake_stripe(captured))

    url = stripe_publisher.publish_listing(name)

    paths = [r[1] for r in captured["requests"]]
    assert paths == ["/products/search", "/products"]
    create = captured["requests"][1][2]
    assert create["idempotency_key"] == "product-q7-creator-media-kit"
    data = create["data"]
    assert data["default_price_data"] == {"currency": "usd", "unit_amount": 1200}
    assert data["metadata"]["queue_id"] == "7"
    assert payload["template_link"] not in json.dumps(data)
    assert data["images"] == ["https://shop.test/p/creator-media-kit/1.jpg"]
    assert url == "https://shop.test/p/creator-media-kit/"
    (_args, kwargs), = captured["listings"]
    assert kwargs["template_link"] == payload["template_link"]
    assert kwargs["status"] == "draft"
    assert kwargs["stripe_product_id"] == "prod_555"
    assert kwargs["payment_link_url"] is None
    assert captured["queue"] == [(7, "published")]


def test_publish_active_creates_payment_link(staged_product, monkeypatch) -> None:
    name, _payload, _assets, captured = staged_product
    monkeypatch.setenv("STORE_PUBLISH_STATUS", "active")
    monkeypatch.setattr(stripe_publisher, "request", _fake_stripe(captured))

    stripe_publisher.publish_listing(name)

    method, path, kwargs = captured["requests"][-1]
    assert (method, path) == ("POST", "/payment_links")
    assert kwargs["data"]["line_items"] == [{"price": "price_555", "quantity": 1}]
    (_args, listing), = captured["listings"]
    assert listing["status"] == "active"
    assert listing["payment_link_url"] == "https://buy.stripe.com/test_1"


def test_publish_reuses_existing_product_instead_of_duplicating(staged_product, monkeypatch) -> None:
    name, _payload, _assets, captured = staged_product
    existing = {"id": "prod_999", "default_price": {"id": "price_999"}}
    monkeypatch.setattr(stripe_publisher, "request", _fake_stripe(captured, existing))

    stripe_publisher.publish_listing(name)

    assert [r[1] for r in captured["requests"]] == ["/products/search"]
    (_args, kwargs), = captured["listings"]
    assert kwargs["stripe_product_id"] == "prod_999"
    assert kwargs["stripe_price_id"] == "price_999"


def test_publish_refuses_without_template_link(staged_product, monkeypatch) -> None:
    name, payload, assets, captured = staged_product
    payload["template_link"] = ""
    (assets / name / "listing.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(stripe_publisher, "request", _fake_stripe(captured))

    with pytest.raises(RuntimeError, match="No template link"):
        stripe_publisher.publish_listing(name)
    assert captured["requests"] == []


# ── Storefront build ──────────────────────────────────────────────────────────

def test_storefront_lists_only_live_products_and_escapes(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "pipeline.db"
    db.init_db(db_path)
    db.insert_listing("Live", "<Live> Kit", status="active", stripe_product_id="prod_1",
                      payment_link_url="https://buy.stripe.com/live", slug="live-kit",
                      price=12.0, currency="usd", description="Great\n- <b>x</b>",
                      tags=json.dumps(["kit"]), images=json.dumps(["p/live-kit/1.jpg"]),
                      template_link="https://canva.link/secret", db_path=db_path)
    db.insert_listing("Draft", "Draft Kit", status="draft", stripe_product_id="prod_2",
                      slug="draft-kit", price=9.0, db_path=db_path)
    monkeypatch.setattr(storefront, "storefront_listings", lambda: db.storefront_listings(db_path))
    monkeypatch.setenv("STOREFRONT_BASE_URL", "https://shop.test")
    assets = tmp_path / "assets"
    (assets / "p" / "live-kit").mkdir(parents=True)
    (assets / "p" / "live-kit" / "1.jpg").write_bytes(b"jpeg")
    site = tmp_path / "site"
    (site / ".git").mkdir(parents=True)
    (site / "stale.html").write_text("old")

    assert storefront.build(site, assets) == 1

    index = (site / "index.html").read_text()
    page = (site / "p" / "live-kit" / "index.html").read_text()
    assert "&lt;Live&gt; Kit" in index and "<Live>" not in index
    assert "Draft Kit" not in index and not (site / "p" / "draft-kit").exists()
    assert 'href="https://buy.stripe.com/live"' in page
    assert "canva.link/secret" not in page
    assert "&lt;b&gt;x&lt;/b&gt;" in page
    assert (site / "p" / "live-kit" / "1.jpg").exists()
    assert (site / ".git").exists() and not (site / "stale.html").exists()
    assert "https://shop.test/p/live-kit/" in (site / "sitemap.xml").read_text()


# ── Sales sync ────────────────────────────────────────────────────────────────

def test_parse_session_uses_actual_fee_split_by_amount() -> None:
    session = {
        "id": "cs_1", "created": 1793145600, "currency": "usd",
        "payment_intent": {"latest_charge": {"balance_transaction": {"fee": 90}}},
        "line_items": {"data": [
            {"id": "li_a", "amount_total": 2000, "quantity": 1, "description": "Kit A",
             "price": {"product": "prod_a"}},
            {"id": "li_b", "amount_total": 1000, "quantity": 1, "description": "Kit B",
             "price": {"product": "prod_b"}},
        ]},
    }

    sales = sales_tracker.parse_session(session, {"prod_a": "A"})

    assert [s["transaction_id"] for s in sales] == ["stripe_cs_1_li_a", "stripe_cs_1_li_b"]
    assert sales[0]["product_name"] == "A" and sales[1]["product_name"] == ""
    assert sales[0]["gross_amount"] == 20.0 and sales[0]["amount"] == 19.4
    assert sales[1]["amount"] == 9.7
    assert sales[0]["currency"] == "USD"


def test_parse_session_estimates_fee_when_unknown() -> None:
    session = {"id": "cs_2", "line_items": {"data": [
        {"id": "li", "amount_total": 1000, "price": {"product": "prod_a"}}]}}

    (sale,) = sales_tracker.parse_session(session, {})

    assert sale["amount"] == round(10 - (10 * 0.029 + 0.30), 2)
