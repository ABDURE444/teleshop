## Quick deploy (fresh Ubuntu 22.04 / 24.04 server)

Point your domain at the server first, then:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/YOURNAME/teleshop.git
cd teleshop
sudo bash install.sh
```

It asks for your domain, master bot token, Telegram ID and currency, then does
everything else: packages, PostgreSQL, Redis, firewall, Python environment,
HTTPS via Caddy, the systemd service and a nightly backup. Takes about ten
minutes, most of it waiting for downloads.

One manual step remains afterwards — message @BotFather, send `/setdomain`,
pick your master bot, and send your domain. Telegram login will not work until
you do. The installer prints this reminder when it finishes.

Re-running `install.sh` is safe: every step checks whether it has already been
done, and an existing `.env` is left alone.

# Teleshop v2.2

Multi-tenant Telegram commerce for any shop — products, cart, collection time,
payment by screenshot, and a web dashboard. One process, one small VPS.

## What v2.2 adds

**Web dashboard** (`web/`, FastAPI + Jinja2, same process as the bots)
- Sign in with Telegram — no passwords, no auth service. Uses the Login
  Widget; the master bot's domain must be set with `/setdomain` in @BotFather
  and must match `WEB_BASE_URL`.
- Categories, products, prices, descriptions, photo upload, stock toggle.
- Orders view with the pickup-code ticket and "Mark collected".
- Structured payment details: bank, account name, account number, address.
  The account NAME is rendered to customers before they pay.
- **Photos cost nothing to host.** Uploads are pushed through the Bot API and
  only the Telegram `file_id` is kept, so Telegram stores and serves every
  product image. No S3, no R2, no egress bill, no bytes on your disk.

**Free trial + self-serve signup**
- A new shop goes live immediately for `TRIAL_DAYS` (default 30) with no
  payment, and the owner gets the dashboard link. One trial per shop, ever.
- Paying during a trial *extends* from the trial's end rather than truncating it.

**Owner commands** — `/today`, `/collect 4821`, `/stock`, `/dashboard`.

**Abandoned part-payments** — an order stuck waiting for its balance is
cancelled after `topup_timeout_minutes` (per shop, default 60). Whatever was
received is recorded as `refund_owed` and shown to the owner, and both sides
are told. The platform never holds funds, so this is visibility, not escrow.

**Collection times are business-generic** — As soon as possible / Today /
Tomorrow / type a day and time. Not lunch slots.

**Out-of-stock** — `products.available`; hidden items vanish from the customer
menu and can't be ordered by a stale button.

**Migrations work on SQLite too** — the schema upgrader now introspects live
columns instead of relying on `ADD COLUMN IF NOT EXISTS`, which SQLite doesn't
support.

## Suggested hosting

One fixed-price VPS (Hetzner CX22, ~€4/mo) running Postgres, Redis and this
process, with Cloudflare's free tier in front for TLS and caching. Avoid
usage-based hosting: this app's cost should never scale with traffic.

# Teleshop v2

A multi-tenant Telegram shop platform: merchants create shops through a **master bot**, each shop gets its **own bot**, and everything runs in **one Python process**.

## What changed from v1 (the refactor)

| v1 | v2 |
|---|---|
| One OS subprocess per shop, generated from a 2,400-line template file, tracked by PID with pgrep/pkill | One asyncio polling task per shop bot, all sharing one Dispatcher, managed by `core/bot_manager.py` |
| Each shop process opened its own DB pool (5+2 connections each) | One shared engine/pool for everything |
| Admin flow state in in-memory dicts — lost on every restart | Redis-backed aiogram FSM — survives restarts |
| Handler classes taking ~15 constructor kwargs | One `AppContext` injected by aiogram (`ctx: AppContext` in any handler) |
| Payments hard-coded to Telegram Stars | Provider adapter layer (`payments/`) — Stars implemented, manual bank/wallet transfer scaffolded for CBE/Telebirr/Dashen verification |
| No orders — products were browse-only | `Order` model + order flow with payment reference submission and admin verification |
| One log file per shop process | One rotating log; every shop line tagged `teleshop.shop.<shop_id>` — `grep` isolates any shop |
| Prices/currency USD/XTR defaults | ETB defaults, per-shop currency in settings |

## Architecture

```
app.py
 ├── master Bot ── master Dispatcher ── master/handlers/*  (create shops, subscriptions, affiliate)
 ├── BotManager ── shop Bot ×N ── ONE shop Dispatcher ── shopbot/handlers/*
 │                    └── ShopContextMiddleware: bot.id → shop_id injected into every handler
 └── BackgroundTasks: expirations · reminders · affiliate-payment monitor · consistency check
```

- **Starting a shop** = `bot_manager.start_shop(shop_id, token)` → validates the token with `get_me`, drops webhooks, spawns a cancellable polling task (`core/polling.py`).
- **Stopping a shop** = cancelling its task. No PIDs, no pgrep, ever.
- The **consistency check** (every 5 min) restarts bots for active shops that aren't running and stops bots for shops that lost their subscription — this replaces v1's entire process-monitoring apparatus.
- Revoked tokens are detected mid-flight (`TelegramUnauthorizedError`), the shop is paused, and the owner is notified.

## Business rules preserved from v1

- Subscription: yearly, paid in Telegram Stars to the master bot (`SUBSCRIPTION_PRICE_STARS`, default 1300).
- Affiliate referral links: `https://t.me/<master>?start=affiliate_<user_id>_shop_<shop_id>` (same payload format — old links keep working).
- A referred shop's paid subscription earns the affiliate `AFFILIATE_COMMISSION_CREDITS` (300) on the **shop that generated the link**.
- At `AFFILIATE_PAYOUT_THRESHOLD` (1300) credits, buyers are redirected to pay **the affiliate's shop bot** directly; the affiliate keeps the Stars, the master burns exactly 300 commission credits, activates the shop atomically (row locks + audit trail), and launches its bot. Retries with a cap; the super admin is alerted on repeated failures.
- Payment safety: a user who paid **always** gets an activated shop, even if the bot launch fails; an emergency recovery path force-activates on unexpected errors.
- Token integrity: tokens are mirrored in Redis with salt+SHA256 and verified before any launch; the Redis copy self-heals from the DB on startup.

## New: orders (per-shop commerce)

Customers tap **🛒 Order** on a product → shop admins are notified instantly → the customer sees the shop's **payment instructions** (set via `/admin → 💳 Payment Settings`, e.g. CBE/Telebirr/Dashen account details) → customer submits a transaction reference → admins **verify & mark paid** (or reject) with one tap.

### Plugging in automatic verification (VerifyCBE-style engine)

`payments/manual_transfer.py` exposes the seam:

```python
async def my_verifier(reference: str) -> VerificationResult:
    # call your verification engine: OCR/dedup/receiver-match/freshness
    ...

provider = ManualTransferProvider(verifier=my_verifier)
```

Until a verifier is attached, everything routes to human confirmation — the system is fully usable without it. The hook point in the order flow is marked in `shopbot/handlers/customer.py` (`payment_reference_received`).

## Running

```bash
cp .env.example .env   # fill in BOT_TOKEN, DATABASE_URL, SUPER_ADMIN_ID
pip install -r requirements.txt
python app.py
```

Requires PostgreSQL and Redis. For local experiments SQLite works too:
`DATABASE_URL=sqlite+aiosqlite:///teleshop.db` (add `aiosqlite` to requirements).

## Migrating a v1 database

Schema upgrades are applied automatically at startup (`services/database_service.py`):
new `orders` table, `shop_settings.payment_instructions` column, and the `pid`
column dropped. v1 data (shops, products, affiliates, payments, audit) is
untouched and fully compatible. The `shops/` directory of generated scripts is
no longer used and can be deleted from the server.

## Debugging cheatsheet

- All logs: `tail -f logs/teleshop.log`
- One shop only: `grep "shop.<shop_id>" logs/teleshop.log` or `grep "POLL:shop:<shop_id>"`
- Payment audit trail: `SELECT * FROM payment_audit ORDER BY timestamp DESC;`
- Stuck affiliate payments: `SELECT * FROM affiliate_payments WHERE status='paid' AND shop_activated=false;`

## Project layout

```
app.py                  entry point + wiring
config.py               typed env config
context.py              AppContext (DI)
core/                   polling loop, BotManager, logging
models/                 SQLAlchemy models (+ Order in v2)
services/               shop / payment / affiliate / order / cache / background tasks
payments/               provider adapters: Stars (live), manual transfer (scaffold)
master/                 master-bot routers & FSM states
shopbot/                shared shop-bot routers, FSM states, shop-context middleware
```
