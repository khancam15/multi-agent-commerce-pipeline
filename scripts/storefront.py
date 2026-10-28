"""
storefront.py — Free static storefront
───────────────────────────────────────
Builds a small static shop from the live listings in SQLite: a home page
with every product, one page per product with its images, description and
a "Buy now" button that opens the Stripe payment link, plus sitemap.xml and
robots.txt. Drafts and products without a payment link are left out.

The site needs no server, so it can be hosted free on GitHub Pages,
Cloudflare Pages or Netlify.

Run:
    python scripts/storefront.py build      — write the site to site/
    python scripts/storefront.py deploy     — build, then push site/ to git

Settings read from .env:
    BRAND_NAME             — store name
    STOREFRONT_BASE_URL    — public URL, e.g. https://you.github.io/store
    STOREFRONT_TAGLINE     — one line under the store name (optional)
    SUPPORT_EMAIL          — shown in the footer (defaults to EMAIL_FROM)
    STOREFRONT_GIT_REMOTE  — git URL to push the built site to (for deploy)
    STOREFRONT_GIT_BRANCH  — branch to push, default gh-pages

Images are stored at outputs/storefront/assets/p/<slug>/ when a product is
published, and copied into site/ on every build.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from db import get_conn, log_run, storefront_listings

_ROOT      = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

ASSETS_DIR = _ROOT / "outputs" / "storefront" / "assets"
SITE_DIR   = _ROOT / "site"
WEB_IMAGE_WIDTH = 1200

CURRENCY_SYMBOLS = {"usd": "$", "cad": "CA$", "aud": "A$", "eur": "€", "gbp": "£"}


# ── Text helpers ──────────────────────────────────────────────────────────────

def slugify(text: str, max_length: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text[:max_length].rstrip("-") or "product"


def unique_slug(title: str) -> str:
    """Slug for a new product that no existing listing uses."""
    base = slugify(title.split("|")[0])
    with get_conn() as conn:
        try:
            taken = {r["slug"] for r in conn.execute("SELECT slug FROM listings").fetchall()}
        except Exception:  # older database without the slug column
            taken = set()
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def plain_text(text: str) -> str:
    """Description without markdown markers, for Stripe and meta tags."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    text = re.sub(r"(?m)^#+\s*", "", text)
    return text.strip()


def to_html(text: str) -> str:
    """
    Convert a plain-text product description to basic HTML.
    All text is escaped: descriptions are written by a model, and nothing
    it outputs should reach the storefront as markup.
    """
    html_parts: list[str] = []
    open_list = ""

    def close_list() -> None:
        nonlocal open_list
        if open_list:
            html_parts.append(f"</{open_list}>")
            open_list = ""

    def start_list(tag: str) -> None:
        nonlocal open_list
        if open_list != tag:
            close_list()
            html_parts.append(f"<{tag}>")
            open_list = tag

    for line in (text or "").split("\n"):
        stripped = line.strip()
        if not stripped:
            close_list()
            continue
        if stripped.startswith(("- ", "• ", "* ")):
            start_list("ul")
            html_parts.append(f"  <li>{html.escape(stripped[2:].strip())}</li>")
        elif stripped[:1].isdigit() and "." in stripped[:3]:
            start_list("ol")
            html_parts.append(f"  <li>{html.escape(stripped.split('.', 1)[1].strip())}</li>")
        elif stripped.startswith("**") and stripped.endswith("**"):
            close_list()
            html_parts.append(f"<h3>{html.escape(stripped.strip('*'))}</h3>")
        elif stripped.startswith("#"):
            close_list()
            html_parts.append(f"<h3>{html.escape(stripped.lstrip('#').strip())}</h3>")
        else:
            close_list()
            html_parts.append(f"<p>{html.escape(stripped)}</p>")
    close_list()
    return "\n".join(html_parts)


def format_price(amount: float, cur: str) -> str:
    symbol = CURRENCY_SYMBOLS.get((cur or "usd").lower())
    return f"{symbol}{amount:.2f}" if symbol else f"{amount:.2f} {cur.upper()}"


def base_url() -> str:
    return os.getenv("STOREFRONT_BASE_URL", "").strip().rstrip("/")


def product_page_url(slug: str) -> str:
    base = base_url()
    return f"{base}/p/{slug}/" if base else ""


# ── Images ────────────────────────────────────────────────────────────────────

def save_web_images(slug: str, image_paths: list[Path]) -> list[str]:
    """
    Save web-sized JPEG copies for the storefront.
    Returns site-relative paths such as p/<slug>/1.jpg.
    """
    from PIL import Image

    out_dir = ASSETS_DIR / "p" / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for i, src in enumerate(image_paths[:10], start=1):
        dest = out_dir / f"{i}.jpg"
        with Image.open(src) as img:
            img = img.convert("RGB")
            if img.width > WEB_IMAGE_WIDTH:
                height = round(img.height * WEB_IMAGE_WIDTH / img.width)
                img = img.resize((WEB_IMAGE_WIDTH, height), Image.LANCZOS)
            img.save(dest, "JPEG", quality=85, optimize=True, progressive=True)
        saved.append(f"p/{slug}/{i}.jpg")
    return saved


# ── Page templates ────────────────────────────────────────────────────────────

CSS = """
:root{--bg:#fbfbf9;--surface:#fff;--fg:#1d1f22;--muted:#5d636b;--line:#e4e4df;--accent:#2f5d50;--accent-fg:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#131518;--surface:#1b1e22;--fg:#eceae6;--muted:#a3a7ad;--line:#2c3036;--accent:#7cc0a9;--accent-fg:#0f1a16}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:inherit}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px}
header.site{border-bottom:1px solid var(--line);background:var(--surface)}
header.site .wrap{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding-block:18px;flex-wrap:wrap}
.brand{font-weight:700;font-size:20px;text-decoration:none;letter-spacing:-.01em}
.tagline{color:var(--muted);font-size:14px}
main{padding-block:36px 56px}
h1{font-size:clamp(26px,4vw,36px);line-height:1.2;margin:0 0 12px;letter-spacing:-.015em}
h2{font-size:20px;margin:0}
h3{font-size:17px;margin:20px 0 6px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:22px;margin-top:24px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden;text-decoration:none;display:flex;flex-direction:column}
.card img{width:100%;aspect-ratio:4/5;object-fit:cover;background:var(--line)}
.card .body{padding:14px 16px;display:grid;gap:6px}
.card .title{font-weight:600;line-height:1.35}
.price{font-weight:700;font-variant-numeric:tabular-nums}
.product{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:40px;align-items:start}
@media (max-width:760px){.product{grid-template-columns:1fr;gap:24px}}
.gallery{display:grid;gap:12px}
.gallery img{width:100%;border-radius:8px;border:1px solid var(--line);background:var(--line)}
.buy{display:inline-block;background:var(--accent);color:var(--accent-fg);text-decoration:none;font-weight:600;padding:14px 26px;border-radius:8px;margin:14px 0 6px}
.buy:hover{filter:brightness(1.08)}
.buy:focus-visible{outline:3px solid var(--fg);outline-offset:2px}
.note{color:var(--muted);font-size:14px}
.desc{margin-top:22px;border-top:1px solid var(--line);padding-top:10px}
.desc ul,.desc ol{padding-left:20px}
footer{border-top:1px solid var(--line);color:var(--muted);font-size:14px}
footer .wrap{padding-block:22px}
.empty{color:var(--muted)}
"""


def _page(title: str, body: str, *, description: str = "", canonical: str = "",
          image: str = "", extra_head: str = "", root: str = "") -> str:
    brand   = os.getenv("BRAND_NAME", "Store")
    tagline = os.getenv("STOREFRONT_TAGLINE", "")
    support = os.getenv("SUPPORT_EMAIL") or os.getenv("EMAIL_FROM", "")
    e = html.escape
    meta = [f'<meta name="description" content="{e(description[:300], quote=True)}">'] if description else []
    if canonical:
        meta.append(f'<link rel="canonical" href="{e(canonical, quote=True)}">')
    meta += [
        f'<meta property="og:title" content="{e(title, quote=True)}">',
        '<meta property="og:type" content="website">',
    ]
    if description:
        meta.append(f'<meta property="og:description" content="{e(description[:300], quote=True)}">')
    if image:
        meta.append(f'<meta property="og:image" content="{e(image, quote=True)}">')
    support_line = f' Questions? Email {e(support)}.' if support else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
{chr(10).join(meta)}
{extra_head}
<style>{CSS}</style>
</head>
<body>
<header class="site"><div class="wrap">
  <a class="brand" href="{root}./">{e(brand)}</a>
  {f'<span class="tagline">{e(tagline)}</span>' if tagline else ''}
</div></header>
<main><div class="wrap">
{body}
</div></main>
<footer><div class="wrap">
  Digital products, delivered by email right after checkout. Payments are processed securely by Stripe.{support_line}
</div></footer>
</body>
</html>
"""


def _listing_view(row) -> dict:
    images = json.loads(row["images"] or "[]")
    return {
        "title": row["title"],
        "slug": row["slug"],
        "price": format_price(float(row["price"] or 0), row["currency"] or "usd"),
        "amount": float(row["price"] or 0),
        "currency": (row["currency"] or "usd").upper(),
        "buy_url": row["payment_link_url"],
        "images": images,
        "description": row["description"] or "",
        "seo_title": row["seo_title"] or row["title"],
        "seo_description": row["seo_description"] or plain_text(row["description"] or "")[:160],
        "tags": json.loads(row["tags"] or "[]"),
    }


def _card(p: dict) -> str:
    e = html.escape
    image = ""
    if p["images"]:
        image = f'<img src="{e(p["images"][0], quote=True)}" alt="{e(p["title"], quote=True)}" loading="lazy">'
    return (
        f'<a class="card" href="p/{e(p["slug"], quote=True)}/">\n  {image}\n'
        f'  <div class="body"><span class="title">{e(p["title"])}</span>'
        f'<span class="price">{e(p["price"])}</span></div>\n</a>'
    )


def render_index(products: list[dict]) -> str:
    e = html.escape
    brand = os.getenv("BRAND_NAME", "Store")
    if products:
        cards = "\n".join(_card(p) for p in products)
        body = f"<h1>{e(brand)}</h1>\n<div class=\"grid\">\n{cards}\n</div>"
    else:
        body = f'<h1>{e(brand)}</h1>\n<p class="empty">New products are on the way. Check back soon.</p>'
    tagline = os.getenv("STOREFRONT_TAGLINE", "")
    return _page(brand, body, description=tagline, canonical=f"{base_url()}/" if base_url() else "")


def render_product(p: dict) -> str:
    e = html.escape
    base = base_url()
    lazy = ' loading="lazy"'
    gallery = "\n".join(
        f'<img src="../../{e(src, quote=True)}" alt="{e(p["title"], quote=True)} — image {i}"'
        f'{lazy if i > 1 else ""}>'
        for i, src in enumerate(p["images"], start=1)
    )
    ld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": p["title"],
        "description": p["seo_description"],
        "image": [f"{base}/{src}" for src in p["images"]] if base else [],
        "offers": {
            "@type": "Offer",
            "price": f"{p['amount']:.2f}",
            "priceCurrency": p["currency"],
            "availability": "https://schema.org/InStock",
            "url": p["buy_url"],
        },
    }
    keywords = ", ".join(p["tags"][:20])
    extra = (f'<meta name="keywords" content="{e(keywords, quote=True)}">\n' if keywords else "") + (
        '<script type="application/ld+json">'
        + json.dumps(ld, ensure_ascii=False).replace("</", "<\\/")
        + "</script>"
    )
    body = f"""<div class="product">
  <div class="gallery">
{gallery}
  </div>
  <div>
    <h1>{e(p["title"])}</h1>
    <div class="price">{e(p["price"])}</div>
    <a class="buy" href="{e(p["buy_url"], quote=True)}">Buy now</a>
    <p class="note">Secure checkout with Stripe. Your Canva template link arrives by email right after payment.</p>
    <div class="desc">
{to_html(p["description"])}
    </div>
  </div>
</div>"""
    return _page(
        p["seo_title"], body,
        description=p["seo_description"],
        canonical=product_page_url(p["slug"]),
        image=f"{base}/{p['images'][0]}" if base and p["images"] else "",
        extra_head=extra,
        root="../../",
    )


# ── Build and deploy ──────────────────────────────────────────────────────────

def build(site_dir: Path = SITE_DIR, assets_dir: Path = ASSETS_DIR) -> int:
    """Write the whole site to site_dir. Returns the number of products listed."""
    products = [_listing_view(r) for r in storefront_listings()]

    if site_dir.exists():
        for child in site_dir.iterdir():
            if child.name == ".git":
                continue  # keep deploy history
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    site_dir.mkdir(parents=True, exist_ok=True)

    for p in products:
        src = assets_dir / "p" / p["slug"]
        page_dir = site_dir / "p" / p["slug"]
        page_dir.mkdir(parents=True, exist_ok=True)
        if src.exists():
            for img in src.glob("*.jpg"):
                shutil.copy2(img, page_dir / img.name)
        (page_dir / "index.html").write_text(render_product(p), encoding="utf-8")

    (site_dir / "index.html").write_text(render_index(products), encoding="utf-8")
    (site_dir / ".nojekyll").write_text("", encoding="utf-8")  # GitHub Pages: serve as-is

    base = base_url()
    robots = "User-agent: *\nAllow: /\n"
    if base:
        today = datetime.now(UTC).date().isoformat()
        urls = [f"{base}/"] + [product_page_url(p["slug"]) for p in products]
        sitemap = "\n".join(
            f"  <url><loc>{html.escape(u)}</loc><lastmod>{today}</lastmod></url>" for u in urls
        )
        (site_dir / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{sitemap}\n</urlset>\n",
            encoding="utf-8",
        )
        robots += f"Sitemap: {base}/sitemap.xml\n"
    (site_dir / "robots.txt").write_text(robots, encoding="utf-8")

    print(f"  [store] Built storefront with {len(products)} product(s) → {site_dir}")
    return len(products)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def deploy(site_dir: Path = SITE_DIR) -> bool:
    """
    Push the built site to STOREFRONT_GIT_REMOTE (branch STOREFRONT_GIT_BRANCH).
    The site folder is its own small git repository, separate from this one.
    Returns False when no remote is configured.
    """
    remote = os.getenv("STOREFRONT_GIT_REMOTE", "").strip()
    branch = os.getenv("STOREFRONT_GIT_BRANCH", "gh-pages").strip() or "gh-pages"
    if not remote:
        print("  [store] STOREFRONT_GIT_REMOTE not set — site built locally only")
        return False

    if not (site_dir / ".git").exists():
        _git("init", "-q", cwd=site_dir)
    _git("checkout", "-q", "-B", branch, cwd=site_dir)
    _git("add", "-A", cwd=site_dir)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=site_dir,
                            capture_output=True, text=True, check=True).stdout
    if status.strip():
        _git("-c", "user.name=Commerce Pipeline", "-c", "user.email=pipeline@localhost",
             "commit", "-q", "-m", f"Update storefront {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
             cwd=site_dir)
    # The site repo is generated output, so its branch is replaced each time
    _git("push", "-q", "--force", remote, f"{branch}:{branch}", cwd=site_dir)
    print(f"  [store] Deployed to {branch}")
    return True


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "build"
    try:
        count = build()
        if command == "deploy":
            deploy()
        log_run("pipeline", "storefront", "success", f"{command}: {count} product(s)")
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        print(f"[error] Storefront {command} failed: {detail}")
        log_run("pipeline", "storefront", "failed", str(detail)[:400])
        sys.exit(1)
