from __future__ import annotations

import logging
import signal
import sys
import time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from addons.lidl.client import LidlClient
from addons.lidl.config import get_settings
from addons.lidl.sync import sync_lidl_receipts
from addons.receipts.db import init_receipt_tables, make_session_factory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("lidl-sync")


def run_once(*, full: bool = False, max_pages: int | None = None) -> int:
    cfg = get_settings()
    SessionLocal, engine = make_session_factory(cfg.database_url)
    init_receipt_tables(engine)
    db = SessionLocal()
    try:
        with LidlClient(
            country=cfg.lidl_country,
            language=cfg.lidl_language,
            device_id=cfg.lidl_device_id,
            app_version=cfg.lidl_app_version,
        ) as client:
            result = sync_lidl_receipts(
                db,
                client,
                bootstrap_refresh_token=cfg.lidl_refresh_token,
                full=full,
                max_pages=max_pages,
            )
        logger.info("Lidl sync: %s", result.message)
        return 1 if result.errors and result.inserted == 0 and result.fetched == 0 else 0
    finally:
        db.close()
        engine.dispose()


def _job() -> None:
    try:
        run_once(full=False)
    except Exception:
        logger.exception("Scheduled Lidl sync failed")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--once" in argv:
        full = "--full" in argv
        max_pages = None
        if "--pages" in argv:
            idx = argv.index("--pages")
            max_pages = int(argv[idx + 1])
        return run_once(full=full, max_pages=max_pages)

    cfg = get_settings()
    tz = ZoneInfo(cfg.app_tz)
    scheduler = BackgroundScheduler(timezone=tz)
    scheduler.add_job(
        _job,
        IntervalTrigger(hours=max(1, cfg.lidl_sync_interval_hours), timezone=tz),
        id="sync_lidl",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info(
        "lidl-sync started (every %sh, country=%s)",
        cfg.lidl_sync_interval_hours,
        cfg.lidl_country,
    )
    # Run immediately on boot
    _job()

    stop = False

    def _stop(*_args: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stop:
        time.sleep(1)

    scheduler.shutdown(wait=False)
    logger.info("lidl-sync stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
