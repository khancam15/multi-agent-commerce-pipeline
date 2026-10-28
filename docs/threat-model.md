# Threat Model

## Scope
The autonomous pipeline on a single Linux VPS: `loop.sh` and the Python
phases in `scripts/`, which call the Anthropic API, Canva (remote MCP and
Connect API), the Shopify Admin API and an SMTP server. There is no browser
automation and no shell execution driven by model output.

## Assets
- Shopify Admin API token: can create, edit and publish products and read orders.
- Canva OAuth tokens and the "Use template" links: the template link **is** the
  paid product. Anyone holding it gets the product.
- Buyer email addresses read from Shopify orders.
- Anthropic, Serper and SMTP credentials in `.env`.
- `outputs/pipeline.db`: listings, template links, sales and delivery history.

## Trust boundaries
- Model output (titles, descriptions, tags, product names) is untrusted input.
- Text returned by Canva MCP tools is untrusted input.
- Shopify order data (names, emails, line items) is untrusted input.
- `.env` and the repository are trusted; the VPS user that runs the loop is trusted.

## Threats and controls

| Threat | Control |
|---|---|
| Template link leaks before purchase | The link is never put in the product description. `listing_builder.py` strips any old template block or canva.com link. It is stored in SQLite and emailed only to buyers of paid orders (`order_delivery.py`). `backfill_template_links.py` cleans products published before this change. |
| Product published with nothing to deliver | Phase 3B fails without a real template link (no preview-URL fallback) and Phase 5 refuses to publish without one. |
| Model writes markup or misleading copy onto the storefront | All description text is HTML-escaped. Products are created as drafts by default and go live only through `./run.sh approve`. Image prompts forbid invented reviews, results and outcome promises. |
| Duplicate products from retries | POST requests are not retried after timeouts or 5xx. The uploader checks Shopify for an existing product with the same title before creating one. |
| Model-chosen product name used as a path | `loop.sh` skips product names containing `/`, `\` or `..`. |
| Secrets executed or leaked through `.env` | `.env` is read with python-dotenv, never sourced as shell. gitleaks runs in CI and as a pre-commit hook. |
| Runaway retries against APIs | systemd stops on configuration errors (exit 78) and gives up after 5 crashes in 10 minutes. |
| Silent failure | Alert email after a phase fails twice in a row, daily health dashboard and digest. |
| Data loss | Daily SQLite backups to `backups/` (14 days kept). |
| Vulnerable dependencies | pip-audit in CI; core AI packages pinned. |

## Residual risks
- The loop runs as root in the documented setup. A dedicated user limits the damage of any compromise.
- Backups stay on the same VPS until they are copied elsewhere.
- Delivery depends on SMTP and on Shopify exposing the buyer email to the app (protected customer data access).
- A buyer can forward their template link. Canva links cannot be limited to one person.
- With `SHOPIFY_PUBLISH_STATUS=active`, model copy goes live without review.

## Operational guidance
- Keep `SHOPIFY_PUBLISH_STATUS=draft` and review drafts before approving them.
- Rotate the Shopify and Canva tokens if the VPS or `.env` may have been exposed.
- Check `./run.sh phase7` for undelivered orders, failed queue items and drafts waiting for review.
