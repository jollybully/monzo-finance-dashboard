from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.routers import api, dashboard, settings as settings_router
from app.services.balance import get_or_create_settings
from app.services.reports import catch_up_missed_reports, generate_and_send_report
from app.services.sheets_sync import sync_transactions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def _job_sync() -> None:
    db = SessionLocal()
    try:
        result = sync_transactions(db)
        logger.info("Scheduled sync: %s", result.message)
    except Exception:
        logger.exception("Scheduled sync failed")
    finally:
        db.close()


def _job_report(period: str) -> None:
    db = SessionLocal()
    try:
        run = generate_and_send_report(db, period, send=True)
        logger.info("Scheduled %s report: id=%s status=%s", period, run.id, run.status)
    except Exception:
        logger.exception("Scheduled %s report failed", period)
    finally:
        db.close()


def _configure_scheduler() -> None:
    cfg = get_settings()
    tz = ZoneInfo(cfg.app_tz)

    scheduler.add_job(
        _job_sync,
        IntervalTrigger(minutes=cfg.sync_interval_minutes, timezone=tz),
        id="sync_sheets",
        replace_existing=True,
        max_instances=1,
    )

    if cfg.report_daily_enabled:
        scheduler.add_job(
            _job_report,
            CronTrigger(
                hour=cfg.report_daily_hour,
                minute=cfg.report_daily_minute,
                timezone=tz,
            ),
            kwargs={"period": "daily"},
            id="report_daily",
            replace_existing=True,
        )

    if cfg.report_weekly_enabled:
        scheduler.add_job(
            _job_report,
            CronTrigger(
                day_of_week="mon",
                hour=cfg.report_weekly_hour,
                minute=cfg.report_weekly_minute,
                timezone=tz,
            ),
            kwargs={"period": "weekly"},
            id="report_weekly",
            replace_existing=True,
        )

    if cfg.report_monthly_enabled:
        scheduler.add_job(
            _job_report,
            CronTrigger(
                day=1,
                hour=cfg.report_monthly_hour,
                minute=cfg.report_monthly_minute,
                timezone=tz,
            ),
            kwargs={"period": "monthly"},
            id="report_monthly",
            replace_existing=True,
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        get_or_create_settings(db)
    finally:
        db.close()

    _configure_scheduler()
    scheduler.start()
    logger.info("Scheduler started")

    db = SessionLocal()
    try:
        caught = catch_up_missed_reports(db)
        if caught:
            logger.info("Catch-up sent: %s", ", ".join(caught))
        else:
            logger.info("Catch-up: nothing due")
    except Exception:
        logger.exception("Catch-up failed")
    finally:
        db.close()

    yield
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


app = FastAPI(title="Personal Finance Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(dashboard.router)
app.include_router(api.router)
app.include_router(settings_router.router)


@app.get("/health")
def health():
    return {"status": "ok"}
