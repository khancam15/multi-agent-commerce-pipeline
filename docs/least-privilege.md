# Least privilege on the VPS

The loop holds every credential (Stripe, Gmail, Canva, Anthropic, Serper) and
can publish products, deploy the storefront and email buyers, so one
compromise reaches all of them. Three steps shrink that, smallest first. None
of them change anything until you run them on the VPS.

## 1. Scope secrets per script (one line in `.env`)

Set `AGENT_ENV_SCOPING=true` in `.env`, then `systemctl restart commerce-pipeline`.

`run.sh` and `loop.sh` start every script through `scripts/scoped_env.py`.
With scoping on, each script gets only the secrets listed for it in
`AGENT_SECRETS` and every other secret is set to an empty value, which the
script's own `load_dotenv` will not overwrite.

| Script | Secrets it keeps |
| --- | --- |
| `brand_crew.py` | Anthropic, Serper |
| `listing_generator.py` | Anthropic |
| `canva_image_generator.py`, `canva_product_creator.py` | Anthropic, Canva |
| `seo_analyzer.py` | Serper |
| `stripe_publisher.py`, `approve_listing.py`, `sales_tracker.py`, `stripe_setup.py` | Stripe |
| `order_delivery.py` | Stripe, Gmail app password |
| `email_digest.py`, `alerts.py` | Gmail app password |
| `atlas_feed.py` | Atlas feed token |
| `preflight.py`, `health_check.py` | all (they check every credential) |
| image, listing, validator, storefront, backup, dashboard scripts | none |

What it protects: the model-driven phases (CrewAI, Claude with Canva MCP,
Serper web results) no longer have the Stripe key or the Gmail password in
their environment, logs or anything a tool can read from `os.environ`.

What it does not: the scripts still run as one user that can read `.env`.
Steps 2 and 3 close that.

Check after turning it on: `./run.sh preflight` passes, and
`./run.sh deliver` still sends (it keeps Stripe and Gmail).
To undo, set `AGENT_ENV_SCOPING=false` and restart.

## 2. Run the loop as a non-root user

```bash
# as root on the VPS
systemctl stop commerce-pipeline
useradd --create-home --shell /usr/sbin/nologin commerce
mv /root/multi-agent-commerce-pipeline /home/commerce/
chown -R commerce:commerce /home/commerce/multi-agent-commerce-pipeline
chmod 600 /home/commerce/multi-agent-commerce-pipeline/.env
# the storefront deploy key moves with it
mkdir -p /home/commerce/.ssh && cp /root/.ssh/<storefront deploy key>* /home/commerce/.ssh/
cp /root/.ssh/config /home/commerce/.ssh/ 2>/dev/null || true
chown -R commerce:commerce /home/commerce/.ssh && chmod 700 /home/commerce/.ssh
sed -i 's#/root/multi-agent-commerce-pipeline#/home/commerce/multi-agent-commerce-pipeline#g; s#^User=root#User=commerce#' \
  /home/commerce/multi-agent-commerce-pipeline/commerce-pipeline.service
cp /home/commerce/multi-agent-commerce-pipeline/commerce-pipeline.service /etc/systemd/system/
systemctl daemon-reload && systemctl start commerce-pipeline
sudo -u commerce /home/commerce/multi-agent-commerce-pipeline/run.sh preflight
```

The `.venv` keeps working after the move only if it was created with
`--copies`; otherwise recreate it as the new user:
`sudo -u commerce bash -c 'cd ~/multi-agent-commerce-pipeline && rm -rf .venv && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt'`.
Move any cron entries from `crontab -e` (root) to `crontab -u commerce -e`.

## 3. One user and env file per side effect (later)

For full separation, give the two agents that move money or email people
their own users and files: `delivery` (Stripe read + Gmail, runs
`order_delivery.py`) and `publisher` (Stripe write + deploy key, runs
`stripe_publisher.py` and `storefront.py deploy`). The loop user then holds
only Anthropic, Serper and Canva, and starts those two through
`sudo -u <agent>` with a sudoers rule for exactly that command. Use a
separate read-only deploy key for anything that only checks the store repo.
