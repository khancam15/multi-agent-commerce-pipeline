# Threat Model

## Scope
The autonomous pipeline on a single Linux VPS: `loop.sh` and the Python
phases in `scripts/`, which call the Anthropic API, Canva (remote MCP and
Connect API), the Stripe API and an SMTP server, plus the static storefront
it pushes to a git host (GitHub Pages, Cloudflare Pages or Netlify). There is
no browser automation and no shell execution driven by model output.

## Assets
- Stripe secret key: can create products and payment links and read payments.
  It cannot move money out of the account by itself, but it exposes buyer emails.
- Storefront deploy key: can overwrite the public storefront.
- Canva OAuth tokens and the "Use template" links: the template link **is** the
  paid product. Anyone holding it gets the product.
- Buyer email addresses read from Stripe Checkout Sessions.
- Anthropic, Serper and SMTP credentials in `.env`.
- `outputs/pipeline.db`: listings, template links, sales and delivery history.

## Trust boundaries
- Model output (titles, descriptions, tags, product names) is untrusted input.
- Text returned by Canva MCP tools is untrusted input.
- Stripe checkout data (names, emails, line items) is untrusted input.
- `.env` and the repository are trusted; the VPS user that runs the loop is trusted.
- The storefront is public. Nothing in `site/` may be secret.

## Threats and controls

| Threat | Control |
|---|---|
| Template link leaks before purchase | The link is never put in the product description, the Stripe product, or the storefront. `listing_builder.py` strips any old template block or canva.com link. It is stored in SQLite and emailed only to buyers of paid checkouts (`order_delivery.py`). |
| Product published with nothing to deliver | Phase 3B fails without a real template link (no preview-URL fallback) and Phase 5 refuses to publish without one. |
| Model writes markup or misleading copy onto the storefront | All text on storefront pages is HTML-escaped, and JSON-LD is escaped for `</`. Products are drafts by default (no payment link, not on the storefront) and go live only through `./run.sh approve`. Image prompts forbid invented reviews, results and outcome promises. |
| Duplicate products or payment links from retries | Phase 5 searches Stripe for the queue ID before creating, and every Stripe write sends an idempotency key, so Stripe applies a repeated request once. Other POSTs are not retried after timeouts or 5xx. |
| Buyer pays but checkout is not paid yet | Delivery and sales count only sessions with `payment_status` paid. |
| Model-chosen product name used as a path | `loop.sh` skips product names containing `/`, `\` or `..`; storefront paths use a slug of `[a-z0-9-]` only. |
| Secrets executed or leaked through `.env` | `.env` is read with python-dotenv, never sourced as shell. gitleaks runs in CI and as a pre-commit hook. The storefront is built from SQLite fields that hold no secrets and is pushed from its own repo in `site/`. |
| Runaway retries against APIs | systemd stops on configuration errors (exit 78) and gives up after 5 crashes in 10 minutes. |
| Silent failure | Alert email after a phase fails twice in a row, daily health dashboard and digest. |
| Data loss | Daily SQLite backups to `backups/` (14 days kept). |
| Vulnerable dependencies | pip-audit in CI; core AI packages pinned. |

## Residual risks
- The loop runs as root in the documented setup. A dedicated user limits the damage of any compromise. Steps: [least-privilege.md](least-privilege.md). Per-script secret scoping (`AGENT_ENV_SCOPING=true`) keeps the Stripe key and Gmail password out of the model-driven phases.
- Backups stay on the same VPS until they are copied elsewhere.
- Delivery depends on SMTP. If email fails, the buyer has paid and waits until the next successful run.
- A buyer can forward their template link. Canva links cannot be limited to one person.
- With `STORE_PUBLISH_STATUS=active`, model copy goes live without review.
- A full `sk_live_` key gives wide account access; a restricted key limits it.

## Operational guidance
- Keep `STORE_PUBLISH_STATUS=draft` and review drafts (`./run.sh approve --preview`) before approving them.
- Use a restricted Stripe key and a deploy key that can write only to the storefront repo.
- Rotate the Stripe key, deploy key and Canva tokens if the VPS or `.env` may have been exposed.
- Check `./run.sh phase7` for undelivered orders, failed queue items and drafts waiting for review.
