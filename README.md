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

## Stack

FastAPI · SQLAlchemy · PostgreSQL · Google Sheets API · APScheduler · Jinja2 · SMTP · Pushover · Docker Compose

## Quick start

1. Copy env and fill in values:

```bash
cp .env.example .env
```

2. Place a Google service-account JSON at `credentials/service-account.json`.

3. Share your Monzo Google Sheet with the service account email (Viewer is enough). Set `GOOGLE_SHEET_ID` and `GOOGLE_SHEET_RANGE` (e.g. `Personal Account Transactions!A:O`).

4. Configure SMTP (`SMTP_*`, `SMTP_FROM_NAME=Finance`, `EMAIL_TO`) and Pushover (`PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`).

5. Start:

```bash
docker compose up --build
```

Open [http://localhost:8000](http://localhost:8000).

6. Click **Sync now** on Overview to import history (this does **not** change balance until you seed it).

7. In **Settings**, set **Current balance** to match Monzo once, plus reserved buffer. Add an **income rule** (e.g. Salary, last Friday of month).

8. On **Bills**, add rent (or accept a recurring suggestion) so safe spend reserves it until payday.

## Safe spend

```text
available = balance − reserved_buffer − bills due by payday
safe_daily = available / days until payday
```

## Digests

| Cadence | Default time | Content | Channels |
|---------|--------------|---------|----------|
| Daily | 07:00 | Yesterday spend + top merchants + tiny today outlook | Pushover + slim email |
| Weekly | Monday | Previous Mon–Sun review | Email + short Pushover |
| Monthly | 1st of month | Previous calendar month | Email + short Pushover |

Mute daily email with `REPORT_DAILY_EMAIL=false` (Pushover still sends). Times follow `APP_TZ`.

If the machine was asleep through a scheduled slot, startup catch-up sends **at most one** missed digest per cadence (daily same day within 18h; weekly Mon–Wed; monthly days 1–3). It does not backfill a multi-day backlog.

## API

- `GET /health`
- `GET /api/safe-spend`
- `POST /api/sync`
- `GET /api/settings` / `PATCH /api/settings`
- `GET /api/reports`
- `POST /api/reports/{daily|weekly|monthly}/send?send=true`

## Security

This repo is meant to be public, but your money data is not.

- **Never commit** `.env` or `credentials/service-account.json` — both are gitignored
- Use `.env.example` as a template only
- Share the Google Sheet **Viewer-only** with the service account email
- Keep SMTP passwords and Pushover tokens in env / a secrets manager in production

## Out of scope (later)

Open Banking direct debits, auto-mark bills paid, AI insights, net worth.
