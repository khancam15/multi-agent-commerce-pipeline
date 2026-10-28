# Multi-Agent Commerce Pipeline

Autonomous pipeline for building digital products, generating Canva mockups and downloadable templates, selling them through Stripe Payment Links on a free static storefront, syncing sales, and running post-publish SEO maintenance.

## Overview

| Layer | Technology |
|---|---|
| Agents | CrewAI agents running on Claude through the Anthropic API |
| Design | Canva remote MCP server, called through the Anthropic API MCP connector, with a Canva Connect API fallback for exports |
| Commerce | Stripe (products, Payment Links, Checkout) and a static storefront hosted free on GitHub Pages, Cloudflare Pages or Netlify |
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
Phase 5   Publish to Stripe        -> Stripe product + price (draft until approved)
Delivery  Email template links     -> buyers of paid checkouts, every 10 minutes
Phase 6   SEO refresh              -> listing tags + meta, storefront rebuilt
Phase 7   Health dashboard         -> stdout/email-ready report
Store     Storefront build/deploy  -> site/ pushed to your Pages branch
```

## How buyers get the product

The Canva "Use template" link is the product, so it never appears on the product page.

1. Phase 3B creates the template link. If it cannot, the product is not published.
2. Phase 5 creates the Stripe product and price as a **draft** and stores the link in SQLite.
   Drafts have no payment link and are not on the storefront.
3. Preview it with `./run.sh approve --preview "Product Name"` (opens as `site_preview/`),
   then run `./run.sh approve "Product Name"`. That creates the Stripe Payment Link and
   rebuilds and deploys the storefront.
4. A buyer clicks **Buy now**, pays on Stripe's checkout page, and `order_delivery.py`
   emails them their link from `EMAIL_FROM`. The loop checks for new payments every 10 minutes.

## Costs

Stripe has no monthly fee; it charges per successful payment (2.9% + 30¢ for US cards
at the time of writing). GitHub Pages, Cloudflare Pages and Netlify host the storefront
for free. Test everything first with a `sk_test_` key and card `4242 4242 4242 4242`;
nothing is charged in test mode.

## Main Commands

```bash
./run.sh db-init
./run.sh stripe-auth
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
./run.sh approve --preview "Name"   # build site_preview/ with the draft included
./run.sh store-build                # rebuild the storefront in site/
./run.sh store-deploy               # rebuild and push it to your Pages branch
./run.sh deliver --dry-run          # show which buyers would be emailed
./run.sh deliver                    # email template links for new paid checkouts
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
STRIPE_SECRET_KEY
STOREFRONT_BASE_URL
STOREFRONT_GIT_REMOTE
EMAIL_FROM
EMAIL_SMTP_PASS
CANVA_CLIENT_ID
CANVA_CLIENT_SECRET
CANVA_ACCESS_TOKEN
CANVA_REFRESH_TOKEN
```

4. Save and verify your Stripe key.

```bash
python scripts/stripe_setup.py
```

5. Initialize SQLite.

```bash
python scripts/db.py
```

## Stripe Setup

1. Create a free Stripe account at stripe.com. You can use test mode before the account is verified.
2. In the Dashboard, open **Developers → API keys** and copy the test secret key (`sk_test_...`).
   A restricted key also works if it can write Products, Prices and Payment Links and read Checkout Sessions.
3. Run `./run.sh stripe-auth` and paste the key. Switch to the live key (`sk_live_...`) when you are ready to sell.

Phase 5 looks up the product by its queue ID before creating anything, and every create sends an
idempotency key, so a retry never makes a duplicate. New products are drafts
(or live straight away with `STORE_PUBLISH_STATUS=active`). A product that fails Phases 3B to 5
three times is marked `failed` in the queue so it does not block the others.

## Storefront Hosting

The storefront is plain HTML built from SQLite into `site/`: a home page, one page per live product
with JSON-LD product data, `sitemap.xml` and `robots.txt`. `./run.sh store-deploy` pushes it as its own
git repository to `STOREFRONT_GIT_BRANCH` (default `gh-pages`) of `STOREFRONT_GIT_REMOTE`.

GitHub Pages (free):

1. Create an empty public repo, e.g. `your-store`.
2. On the VPS, make a deploy key: `ssh-keygen -t ed25519 -f ~/.ssh/store_deploy -N ""`, add the public key
   under the repo's **Settings → Deploy keys** with write access, and point SSH at it for github.com.
3. Set `STOREFRONT_GIT_REMOTE=git@github.com:you/your-store.git` and run `./run.sh store-deploy` once.
4. In **Settings → Pages**, choose "Deploy from a branch" and pick `gh-pages`.
5. Set `STOREFRONT_BASE_URL` to the Pages URL (or your own domain) so links, images and the sitemap are absolute.

Cloudflare Pages or Netlify work the same way: connect them to the same repo and branch, with no build command.

## Database

SQLite lives at `outputs/pipeline.db`.

Core tables:

```text
queue       pending/designed/published product work
listings    published products: Stripe product/price/payment link IDs, slug, template link
run_log     phase status history
seo_review  before/after SEO tag audit trail
sales       Stripe checkout line-item revenue (actual Stripe fee when known)
deliveries  template links emailed to buyers
```

## Services

Install the systemd unit on the VPS. If the old `shopify-pipeline` service is installed,
stop and remove it first:

```bash
systemctl disable --now shopify-pipeline 2>/dev/null; rm -f /etc/systemd/system/shopify-pipeline.service
cp commerce-pipeline.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now commerce-pipeline
systemctl status commerce-pipeline
```

## Verification

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q scripts tests
```
