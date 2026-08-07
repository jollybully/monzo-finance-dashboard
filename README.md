# Personal Finance Dashboard

Self-hosted dashboard for Monzo: sync transactions from Google Sheets, track bills-aware safe daily spend, manage budgets and income rules, and get lean digests by email and Pushover.

## Features

- **Sheets sync** — pull Monzo export on a schedule or with one click
- **Safe daily spend** — balance minus buffer and bills due before payday, divided by days left
- **Pay-period stats** — spent, inflows, and top categories from last payday to next
- **Bills** — recurring commitments plus suggestions scanned from history
- **30-day forecast** — income rules and bills projected forward (not everyday spend)
- **Category budgets** — calendar-month limits with progress at a glance
- **Digests** — daily / weekly / monthly via email and Pushover
- **AI coaching** — optional Gemini insights on Overview and in the weekly digest (pace vs safe daily, leaks, habits)
- **Finance MCP** — read-only Cursor tools for spend/merchant/bill interrogation (pay-period pace, 4- vs 5-week comparisons)
- **Receipt addons (optional)** — separate `lidl-sync` container pulls Lidl Plus line items into shared Postgres; dashboard shows item drill-down under Groceries → Lidl without coupling to core Monzo sync

## Stack

FastAPI · SQLAlchemy · PostgreSQL · Google Sheets API · APScheduler · Jinja2 · SMTP · Pushover · Gemini (optional) · Docker Compose

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker Engine + Compose v2)
- A **Monzo paid plan** with [auto-export to Google Sheets](https://monzo.com/help/monzo-plus/advanced-budgeting-auto-exports) (Extra, Perks, or Max — formerly Plus). Free Monzo can export CSV manually, but this app expects the live sheet Monzo keeps updated.
- A Google account that owns (or can share) that sheet
- A Google Cloud project (free tier is fine) for a service account that can *read* that sheet

SMTP and Pushover are **optional**. The dashboard works with only Docker + Sheets; digests need at least one of email or Pushover.

---

## 1. Get your Monzo data into Google Sheets

In the Monzo app, turn on **auto-export live transactions** (paid plans). Monzo creates a spreadsheet and keeps it updated — leave the columns as Monzo provides them.

Copy two things for `.env`:

1. **Spreadsheet ID** from the URL:

```text
https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit
```

2. **Tab name** (bottom of the sheet) for `GOOGLE_SHEET_RANGE`, e.g. `Personal Account Transactions!A:O` or `Monzo Transactions!A:O`.

---

## 2. Create a Google service account

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create (or pick) a project.
2. Enable **Google Sheets API** for that project (*APIs & Services → Library*).
3. Create a **service account** (*IAM & Admin → Service Accounts → Create*).
4. Create a JSON key for it and download the file.
5. Save it locally as:

```text
credentials/service-account.json
```

(`credentials/` is gitignored except an empty `.gitkeep`.)

6. Open the JSON and copy `client_email` (looks like `…@….iam.gserviceaccount.com`).
7. In Google Sheets, **Share** your Monzo spreadsheet with that email as **Viewer**.

---

## 3. Configure environment

```bash
git clone <this-repo>
cd finance-dashboard
cp .env.example .env
```

Edit `.env` and set at least:

| Variable | What to put |
|----------|-------------|
| `GOOGLE_SHEET_ID` | ID from the spreadsheet URL |
| `GOOGLE_SHEET_RANGE` | Tab + columns, e.g. `Personal Account Transactions!A:O` |
| `APP_TZ` | Your timezone, e.g. `Europe/London` |

`GOOGLE_CREDENTIALS_FILE` defaults to `/credentials/service-account.json` inside the container (Compose mounts `./credentials` there). Leave it unless you change the mount.

### Digests (optional)

**Email (SMTP)** — e.g. Gmail app password or any SMTP relay:

- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`
- `SMTP_FROM`, `SMTP_FROM_NAME`, `EMAIL_TO`
- `SMTP_USE_TLS=true`

**Pushover** — [pushover.net](https://pushover.net) app + user keys:

- `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`
- Optional: `PUSHOVER_DEVICE`, `PUSHOVER_ENABLED=true`

You can use email only, Pushover only, or both. Set `REPORT_DAILY_EMAIL=false` to keep daily Pushover but skip the daily email.

Schedule knobs (defaults shown in `.env.example`): `REPORT_*_HOUR` / `REPORT_*_MINUTE`, `REPORT_*_ENABLED`, `SYNC_INTERVAL_MINUTES`.

### AI coaching (optional)

Get a free API key from [Google AI Studio](https://aistudio.google.com/apikey), then set:

- `GEMINI_API_KEY` — required to show coaching
- `GEMINI_MODEL` — default `gemini-2.5-flash` (free Flash tier)
- `INSIGHTS_ENABLED=true`

Coaching appears on **Overview** (with Refresh) and in the **weekly** digest. Daily digests stay numbers-only. Python computes the facts; Gemini only writes the narrative.

**Privacy:** on the Gemini free tier, prompts may be used by Google to improve products. This app sends aggregates and a few named outliers — not your full transaction history. Keep the key in `.env` only.

---

## 4. Start the stack

```bash
docker compose up --build
```

- App: [http://localhost:8000](http://localhost:8000)
- Health: [http://localhost:8000/health](http://localhost:8000/health)

Postgres is included; Compose overrides `DATABASE_URL` to point at the `postgres` service. Data persists in the `postgres_data` volume.

Stop with `Ctrl+C`, or run detached:

```bash
docker compose up --build -d
docker compose logs -f finance-app
```

### Optional: Lidl Plus receipts

Lidl runs as an isolated addon. If it breaks or you omit it, the finance dashboard is unchanged.

1. Put `LIDL_REFRESH_TOKEN` in `.env` (one-time browser OAuth — see community Lidl Plus login helpers). The token **rotates** on every sync and is also stored in `source_auth`.
2. Start with the addons profile:

```bash
docker compose --profile addons up -d --build
docker compose logs -f lidl-sync
```

3. Open **Spending → Groceries → Lidl** for receipts and top items, or `/receipts/lidl/{id}` / `/receipts/lidl/items/{product_id}`.

---

## 5. First-run setup in the UI

1. **Overview → Sync now** — imports history from the sheet. This does **not** set your balance until you seed it.
2. **Settings → Current balance** — set once to match Monzo (and a reserved buffer if you want a cushion).
3. **Settings → Income rule** — e.g. Salary, last Friday of month (drives payday and safe spend).
4. **Bills** — add rent and other commitments, or **Scan transactions** and accept suggestions. Weekly bills are reserved for *every* occurrence until payday. Name each bill to match Monzo’s **Name** column (accepting a suggestion does this automatically) so those merchants drop out of discretionary spend and top-merchant rankings.
5. Optionally set **Budgets** by Monzo category.

Spend digests (overview, spending, emails, insights) treat Monzo **Bills**, **Savings**, and **Transfers** categories — plus merchants that match active Upcoming Bills — as non-discretionary. Positive **Card payment** amounts (refunds/reversals) net against spend; other credits (salary, Faster payments, pot withdrawals) count as income. Budgets and bill detection still see the full history. Keep pot top-ups and internal transfers in Savings or Transfers in Monzo.

After that, sync runs on `SYNC_INTERVAL_MINUTES` (default 15). Balance only moves for transactions *after* the balance watermark (last applied transaction time, or when you last seeded).

---

## Keeping it running

Compose uses `restart: unless-stopped`. Containers come back when Docker starts, as long as you didn’t `docker compose stop` them.

On a laptop:

1. Enable **Start Docker Desktop when you log in**.
2. Leave the stack running (`docker compose up -d`).

If the machine was asleep through a scheduled digest, **startup catch-up** sends at most **one** missed digest per cadence (daily: same day within 18h; weekly: Mon–Wed; monthly: days 1–3). It does not backfill a multi-day backlog. You can always send manually from **Reports**.

---

## Safe spend

```text
available = balance − reserved_buffer − bills due by payday
safe_daily = available / days until payday
```

Recurring bills (especially weekly) count **every** charge from today through payday, not just the next one.

---

## Digests

| Cadence | Default time | Content | Channels |
|---------|--------------|---------|----------|
| Daily | 07:00 | Yesterday spend + top merchants + tiny today outlook | Pushover + slim email |
| Weekly | Monday | Previous Mon–Sun review + optional Gemini coach | Email + short Pushover |
| Monthly | 1st of month | Previous calendar month | Email + short Pushover |

Times follow `APP_TZ`.

---

## API

- `GET /health`
- `GET /api/safe-spend`
- `POST /api/sync`
- `GET /api/settings` / `PATCH /api/settings`
- `GET /api/reports`
- `POST /api/reports/{daily|weekly|monthly}/send?send=true`

---

## Finance MCP (Cursor)

Read-only MCP server so Cursor can query discretionary spend, merchants, bills, safe spend, and pay-period comparisons (normalised for 4- vs 5-week months).

### Local setup

1. Start the stack (`docker compose up -d`) — Postgres is published on **`127.0.0.1:5432`** only.
2. Install host deps:

```bash
# Use Python 3.12 (matches the Docker image; 3.14 breaks SQLAlchemy 2.0.36)
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-mcp.txt
```

3. Copy [`.cursor/mcp.json.example`](.cursor/mcp.json.example) → `.cursor/mcp.json`, set absolute paths and `DATABASE_URL`, then restart Cursor / reload MCP servers. (`.cursor/mcp.json` is gitignored.)
4. Ask in chat, e.g.:
   - “Top-level health check for this pay period vs last.”
   - “Compare the last 6 pay periods by daily discretionary spend.”
   - “Deep dive Amazon over my full history.”
   - “What’s reserved for bills, and what’s my safe daily?”

Spend tools default to **discretionary** totals (Bills / Savings / Transfers + Upcoming Bill merchants excluded). Name bills to match Monzo’s **Name** column so rent etc. stay out of lifestyle rankings.

### Unraid / always-on box

Run the dashboard + Postgres on Unraid as usual. For Cursor on your Mac:

- Point `DATABASE_URL` in `.cursor/mcp.json` at the Unraid host (LAN IP), e.g. `postgresql+psycopg://finance:…@192.168.x.x:5432/finance`
- Prefer binding Postgres to the LAN/tailnet interface only — **not** the public internet
- The MCP process still runs **where Cursor is** (stdio); only the DB needs to be reachable
- Later: an HTTP MCP sidecar on Unraid is optional if you want agents on the box itself

---

## Security

This repo is meant to be public; your money data is not.

- **Never commit** `.env` or `credentials/service-account.json` (gitignored)
- Use `.env.example` as a template only
- Share the Google Sheet **Viewer-only** with the service account email
- Keep SMTP passwords and Pushover tokens out of git; use a secrets manager in production
- Keep Postgres / MCP on localhost or a private network only; the app has no auth

---

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Sync fails / no rows | Sheet shared with SA email? Correct `GOOGLE_SHEET_ID` / tab name in `GOOGLE_SHEET_RANGE`? JSON at `credentials/service-account.json`? |
| Digests not arriving | SMTP and/or Pushover filled in? `REPORT_*_ENABLED`? App timezone? Try **Reports → Send daily** |
| Balance looks wrong | Re-seed **Current balance** in Settings to match Monzo. Sync advances the watermark to the latest *applied* transaction time (not wall clock); late sheet rows and card settlement amount changes are applied. Existing drift does not self-heal. |
| Port 8000 in use | Change the host port in `docker-compose.yml` (`"8001:8000"`) |

---

## Out of scope (later)

Open Banking direct debits, auto-mark bills paid, net worth.
