"""
Scheduler — runs the scraper every day at SCRAPE_HOUR:SCRAPE_MINUTE (UTC).
Also exposes run_scrape_job() so the API can trigger it manually.
"""

import asyncio
import traceback

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import AsyncSessionLocal
from app.scraper import run_scraper
from app.crud import create_scrape_run, finish_scrape_run, upsert_listings

scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")

# Track whether a job is already running (prevent overlap)
_running = False


async def run_scrape_job() -> dict:
    """
    Execute a full scrape cycle.
    Returns a summary dict.
    """
    global _running
    if _running:
        return {"status": "skipped", "reason": "A scrape is already running"}

    _running = True
    run_id = None

    async with AsyncSessionLocal() as db:
        try:
            run = await create_scrape_run(db)
            run_id = run.id
            print(f"[scheduler] Scrape run #{run_id} started")

            listings = await run_scraper(settings.OLX_BASE_URL, settings.MAX_PAGES)
            saved = await upsert_listings(db, listings)

            await finish_scrape_run(
                db,
                run_id,
                status="success",
                pages_scraped=settings.MAX_PAGES,
                listings_found=len(listings),
                listings_saved=saved,
            )
            print(f"[scheduler] Run #{run_id} done — {saved} listings saved")
            return {
                "status": "success",
                "run_id": run_id,
                "listings_found": len(listings),
                "listings_saved": saved,
            }

        except Exception as exc:
            tb = traceback.format_exc()
            print(f"[scheduler] Run #{run_id} FAILED:\n{tb}")
            if run_id:
                try:
                    await finish_scrape_run(
                        db, run_id, status="failed", error=str(exc)[:2000]
                    )
                except Exception:
                    pass
            return {"status": "failed", "error": str(exc)}

        finally:
            _running = False


def _sync_job():
    """Bridge between APScheduler (sync callback) and our async job."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(run_scrape_job())
        else:
            loop.run_until_complete(run_scrape_job())
    except RuntimeError:
        asyncio.run(run_scrape_job())


def start_scheduler():
    scheduler.add_job(
        _sync_job,
        trigger=CronTrigger(
            hour=settings.SCRAPE_HOUR,
            minute=settings.SCRAPE_MINUTE,
        ),
        id="daily_olx_scrape",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    print(
        f"[scheduler] Started — daily at "
        f"{settings.SCRAPE_HOUR:02d}:{settings.SCRAPE_MINUTE:02d} Tashkent time"
    )


def stop_scheduler():
    scheduler.shutdown(wait=False)
    print("[scheduler] Stopped")
