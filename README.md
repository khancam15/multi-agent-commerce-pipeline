# Multi-Agent Commerce Pipeline

Autonomous pipeline for building digital products, generating Canva mockups and downloadable templates, publishing products to Shopify, syncing sales, and running post-publish SEO maintenance.

**Audience:** engineers evaluating how CrewAI agents, an MCP server and third party commerce APIs can be composed into a gated, auditable pipeline. Part of [Khaneil Campbell's](https://github.com/khancam15) portfolio; the store it publishes to belongs to Benjaire LLC.

## Status

Working personal project, run on a single Linux VPS by its author. It is not packaged for other stores: adopting it means supplying your own Shopify app, Canva integration and brand guide, and reviewing every publish gate for your own account. See [Limitations](#limitations) and [docs/threat-model.md](docs/threat-model.md).

## Overview

| Layer | Technology |
|---|---|
| Agents | CrewAI agents running on Claude through the Anthropic API |
| Design | Canva remote MCP server, called through the Anthropic API MCP connector, with a Canva Connect API fallback for exports |
| Commerce | Shopify Admin API for product publishing, sales sync, and SEO updates |
| Runtime | Python on a Linux VPS, managed by a systemd service |
| Data | SQLite for run history and product state |
| Quality | pytest unit tests (local), gitleaks secret scanning and pip-audit dependency checks in GitHub Actions |

## Built with AI Assistance

This project was designed and built with Claude as a working partner. Claude helped plan the architecture, write and debug the Python modules, and review the code for security. I defined the requirements, made the design decisions, and reviewed and tested generated code before merging it. API keys and tokens are stored only in a local `.env` file and are never committed or shared.

## Workflow

```text
Phase 1   Brand strategy          -> outputs/brand_guide.md
Phase 2   Product listing plan     -> outputs/master.txt
Phase 3   Canva mockup images      -> 02_Products/<product>/Mockups/
Phase 3B  Canva product/template   -> 03_Canva_Exports/<product>/meta.json
Phase 4   Process, build, validate -> 04_Assets/ReadyToUpload/<product>/
Phase 5   Publish to Shopify       -> Shopify Admin API
Phase 6   SEO refresh              -> Shopify tags + SEO metafields
Phase 7   Health dashboard         -> stdout/email-ready report
```

## Main Commands

All commands run from the project root through `run.sh`, which requires `.venv/` and `.env` to exist and exits otherwise.

| Command | What it does |
|---|---|
| `./run.sh db-init` | Create the SQLite schema (once per machine) |
| `./run.sh preflight` (alias `check`) | Verify credentials and connectivity before a run |
| `./run.sh shopify-auth`, `./run.sh canva-auth` | One time OAuth and token setup; writes tokens to `.env` |
| `./run.sh phase1` | Brand Builder crew, writes `outputs/brand_guide.md` |
| `./run.sh phase2` | Launch Executor, writes `outputs/master.txt` |
| `./run.sh phase2-rich` | Autonomous Executor with the richer prompt engine |
| `./run.sh phase3 "Product"` (alias `design`) | Six Canva mockups through Claude and the Canva MCP server; optional `--price` |
| `./run.sh phase3b "Product"` (alias `product`) | The downloadable Canva template buyers receive |
| `./run.sh phase4 "Product"` | Process images, build the listing, validate, stage |
| `./run.sh phase5 "Product"` | Publish the staged listing to Shopify |
| `./run.sh phase6 "Product"` | SEO gap analysis and tag updates on the live listing |
| `./run.sh phase7` | Daily health dashboard |
| `./run.sh sales [--full]` | Sync Shopify orders and revenue into SQLite |
| `./run.sh digest` | Send the email digest now |
| `./run.sh all` | Phase 1 then Phase 2 |
| `./run.sh full "Product"` | Phase 3, 3B, 4, 5 and 6 for one product |

For the autonomous VPS loop (build, design and maintain modes, gated by `WEEKLY_PUBLISH_LIMIT` and `PUBLISH_COOLDOWN_HOURS`; the header of `loop.sh` documents each mode):

```bash
./loop.sh          # run forever
./loop.sh --once   # one cycle, then exit
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

3. Fill in `.env`. Every variable is documented inline in `.env.example`; in summary:

| Group | Variables |
|---|---|
| Brand and AI | `BRAND_NAME`, `ANTHROPIC_API_KEY`, `SERPER_API_KEY`, `CREWAI_TRACING_ENABLED` |
| Shopify Admin API | `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ACCESS_TOKEN`, `SHOPIFY_API_VERSION`, `SHOPIFY_VENDOR` |
| Canva | `CANVA_CLIENT_ID`, `CANVA_CLIENT_SECRET`, `CANVA_ACCESS_TOKEN`, `CANVA_REFRESH_TOKEN` (filled by `canva-auth`), `CANVA_MCP_TOKEN` (Phase 3) |
| Publishing cadence | `WEEKLY_PUBLISH_LIMIT`, `PUBLISH_COOLDOWN_HOURS` |
| Email digest | `EMAIL_TO`, `EMAIL_FROM`, `EMAIL_SMTP_PASS` (Gmail app password) |

`.env` is gitignored and scanned for by gitleaks in CI; never commit it.

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

The uploader creates an active Shopify product, embeds mockup images, stores the product URL and product ID locally, and marks the queue row as published.

## Database

SQLite lives at `outputs/pipeline.db`.

Core tables:

```text
queue       pending/designed/published product work
listings    published Shopify products, including shopify_url and shopify_product_id
run_log     phase status history
seo_review  before/after SEO tag audit trail
sales       Shopify order line-item revenue
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

The unit file runs the loop as `root` from `/root/multi-agent-commerce-pipeline`; adjust `User=` and the paths to a dedicated service account before installing it on any shared host.

## Verification

Local checks:

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q scripts tests
bash test_infrastructure.sh
```

Automated checks: `.github/workflows/security.yml` runs gitleaks secret scanning and `pip-audit` against `requirements.txt` on every push and pull request, and `.pre-commit-config.yaml` runs gitleaks before each commit. The unit tests are not yet run in CI.

## Limitations

- Built for one store and one brand; multi tenant use is out of scope.
- Phases 3 and 3B depend on Canva's remote MCP server and the Anthropic MCP connector; a change on either side breaks mockup generation until the client code is updated.
- LLM output and page content are treated as untrusted, and publish steps are gated, but the agents still act on a live Shopify store. Run against a development store first.
- Sales sync reads Shopify orders only; there is no accounting export.
- The email digest uses a Gmail app password over SMTP, which is adequate for a single owner and not for a team.

## Documentation

- [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md): what each module does, configuration reference, troubleshooting
- [docs/QUICKREF.md](docs/QUICKREF.md): day to day commands, environment variables, diagnostics
- [docs/threat-model.md](docs/threat-model.md): assets, trust boundaries, implemented controls, residual risks
- [docs/CLEANUP.md](docs/CLEANUP.md): record of the script cleanup and what each remaining module is for
- [CONTRIBUTING.md](CONTRIBUTING.md): branch, commit and pull request conventions
