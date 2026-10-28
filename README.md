# Multi-Agent Commerce Pipeline

Autonomous pipeline for building digital products, generating Canva mockups and downloadable templates, publishing products to Shopify, syncing sales, and running post-publish SEO maintenance.

## Overview

| Layer | Technology |
|---|---|
| Agents | CrewAI agents running on Claude through the Anthropic API |
| Design | Canva remote MCP server, called through the Anthropic API MCP connector, with a Canva Connect API fallback for exports |
| Commerce | Shopify Admin API for product publishing, sales sync, and SEO updates |
| Runtime | Python on a Linux VPS, managed by a systemd service |
| Data | SQLite for run history and product state |
| Quality | Automated tests, gitleaks secret scanning, and pip-audit dependency checks in GitHub Actions |

## Built with AI Assistance

This project was designed and built with Claude as a working partner. Claude helped plan the architecture, write and debug the Python modules, and review the code for security. I defined the requirements, made the design decisions, and reviewed and tested generated code before merging it. API keys and tokens are stored only in a local `.env` file and are never committed or shared.

## Workflow

```text
Phase 1   Brand strategy          -> outputs/brand_guide.md
Phase 2   Product listing plan     -> outputs/master.txt
Phase 3   Canva mockup images      -> 02_Products/<product>/Mockups/
Phase 3B  Canva product/template   -> 03_Canva_Exports/<product>/meta.json
Phase 4   Process, build, validate -> 04_Assets/ReadyToUpload/<product>/
Phase 5   Publish to Shopify       -> Shopify Admin API (draft until approved)
Delivery  Email template links     -> buyers of paid orders, every 10 minutes
Phase 6   SEO refresh              -> Shopify tags + SEO metafields
Phase 7   Health dashboard         -> stdout/email-ready report
```

## How buyers get the product

The Canva "Use template" link is the product, so it never appears on the product page.

1. Phase 3B creates the template link. If it cannot, the product is not published.
2. Phase 5 creates the Shopify product as a **draft** and stores the link in SQLite.
3. You review the draft in Shopify admin, then run `./run.sh approve "Product Name"`.
4. After each paid order, `order_delivery.py` emails the buyer their link from `EMAIL_FROM`.
   The loop checks for new orders every 10 minutes.

For products published before this change, run `./run.sh backfill` to see which
descriptions still contain the link, then `./run.sh backfill --apply` to store the
links and remove them from the live descriptions.

## Main Commands

```bash
./run.sh db-init
./run.sh shopify-auth
./run.sh canva-auth
./run.sh phase1
./run.sh phase2
./run.sh phase3 "Product Name"
./run.sh phase3b "Product Name"
./run.sh phase4 "Product Name"
./run.sh phase5 "Product Name"
./run.sh phase6 "Product Name"
./run.sh phase7
./run.sh sales
./run.sh approve "Product Name"     # make a reviewed draft live (--list shows drafts)
./run.sh deliver --dry-run          # show which buyers would be emailed
./run.sh deliver                    # email template links for new paid orders
./run.sh backfill [--apply]         # fix products published before private delivery
./run.sh backup                     # copy pipeline.db to backups/
./run.sh full "Product Name"
```

For the autonomous VPS loop:

```bash
./loop.sh
./loop.sh --once
```

## Setup

1. Create a virtualenv and install dependencies.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

2. Copy the environment template.

```bash
cp .env.example .env
```

3. Fill in:

```text
ANTHROPIC_API_KEY
SERPER_API_KEY
SHOPIFY_STORE_DOMAIN
SHOPIFY_ACCESS_TOKEN
CANVA_CLIENT_ID
CANVA_CLIENT_SECRET
CANVA_ACCESS_TOKEN
CANVA_REFRESH_TOKEN
```

4. Verify Shopify credentials.

```bash
python scripts/shopify_setup.py
```

5. Initialize SQLite.

```bash
python scripts/db.py
```

## Shopify Requirements

Create a custom Shopify app with Admin API access. Required scopes:

```text
write_products
read_products
read_orders
write_orders
read_inventory
```

Delivery emails need the buyer's email address, so also enable **Protected customer data access → Email** for the app.

The uploader checks that no product with the same title exists, creates a draft product (or active, with `SHOPIFY_PUBLISH_STATUS=active`), embeds mockup images, stores the product URL, product ID and template link locally, and marks the queue row as published. A product that fails Phases 3B to 5 three times is marked `failed` in the queue so it does not block the others.

## Database

SQLite lives at `outputs/pipeline.db`.

Core tables:

```text
queue       pending/designed/published product work
listings    published Shopify products, including shopify_url and shopify_product_id
run_log     phase status history
seo_review  before/after SEO tag audit trail
sales       Shopify order line-item revenue
deliveries  template links emailed to buyers
```

## Services

Install the Shopify systemd unit on the VPS:

```bash
cp shopify-pipeline.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable shopify-pipeline
systemctl start shopify-pipeline
systemctl status shopify-pipeline
```

## Verification

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q scripts tests
```
