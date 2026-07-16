# Personal Finance Dashboard

Self-hosted Phase 1 finance dashboard: sync Monzo transactions from Google Sheets, track balance and safe daily spend, and email daily/weekly/monthly reports.

## Stack

- FastAPI + SQLAlchemy + PostgreSQL
- Google Sheets API (read-only Monzo export)
- APScheduler (sync + reports)
- Jinja2 dashboard + SMTP email

## Quick start

1. Copy env and fill in values:

```bash
cp .env.example .env
```

2. Place a Google service-account JSON at `credentials/service-account.json`.

3. Share your Monzo Google Sheet with the service account email (Viewer is enough). Set `GOOGLE_SHEET_ID` in `.env`. Default range is `Monzo Transactions!A:O`.

4. Configure SMTP (`SMTP_*`, `EMAIL_TO`) for report emails.

5. Start:

```bash
docker compose up --build
```

Open [http://localhost:8000](http://localhost:8000).

6. In **Settings**, seed your current Monzo balance, payday day (1–28), monthly income estimate, and reserved buffer.

7. Click **Sync now** on Overview (or wait for the 15-minute job).

## Monzo sheet columns

The live export (and CSV) uses:

| Header | Stored as |
|--------|-----------|
| Transaction ID | unique key |
| Date / Time / Type | date, time, type |
| Name | merchant |
| Category | category (Monzo categories only) |
| Amount / Currency | amount, currency |
| Notes and #tags / Description | notes, description |

Recategorise in the Monzo app; the next sync updates category/merchant without double-counting amounts.

## Reports

| Period | Default schedule (`Europe/London`) |
|--------|-------------------------------------|
| Daily | 07:00 |
| Weekly | Monday 07:30 (previous Mon–Sun) |
| Monthly | 1st at 08:00 (previous calendar month) |

Send manually from the **Reports** page. History is stored and viewable in the dashboard.

## API

- `GET /health`
- `GET /api/safe-spend`
- `POST /api/sync`
- `GET /api/settings` / `PATCH /api/settings`
- `GET /api/reports`
- `POST /api/reports/{daily|weekly|monthly}/send?send=true`

## Local run (without Docker app)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# start postgres via docker compose up postgres -d
export DATABASE_URL=postgresql+psycopg://finance:finance@localhost:5432/finance
uvicorn app.main:app --reload --port 8000
```

## Out of scope (later)

Budgets, last-Friday income rules, recurring detection, local category overrides, Pushover, AI insights.
