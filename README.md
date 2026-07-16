# Personal Finance Dashboard

Self-hosted finance dashboard: sync Monzo transactions from Google Sheets, track balance and bills-aware safe daily spend, manage income rules and budgets, and send lean daily/weekly/monthly digests via email and Pushover.

## Stack

- FastAPI + SQLAlchemy + PostgreSQL
- Google Sheets API (read-only Monzo export)
- APScheduler (sync + reports)
- Jinja2 dashboard + SMTP email + Pushover

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

8. On **Bills**, add rent (or Accept a recurring suggestion) so safe spend reserves it until payday.

## Digests

| Cadence | Default time | Content | Channels |
|---------|--------------|---------|----------|
| Daily | 07:00 (your `.env` may differ) | Yesterday spend + top merchants + tiny today outlook; watch-outs only if urgent | Pushover + slim email |
| Weekly | Monday | Previous Mon–Sun review | Email + short Pushover |
| Monthly | 1st of month | Previous calendar month | Email + short Pushover |

Daily email can be muted with `REPORT_DAILY_EMAIL=false` (Pushover still sends).

## Phase 2 features

- Income rules, upcoming bills, forecast, budgets, recurring suggestions
- Pay-period stats on Overview/Spending (last payday → next payday)
- Bills-aware safe spend

```text
available = balance − reserved_buffer − bills due by payday
safe_daily = available / days until payday
```

## API

- `GET /health`
- `GET /api/safe-spend`
- `POST /api/sync`
- `GET /api/settings` / `PATCH /api/settings`
- `GET /api/reports`
- `POST /api/reports/{daily|weekly|monthly}/send?send=true`

## Out of scope (later)

Open Banking direct debits / scheduled payments, auto-mark bills paid, AI insights, net worth.
