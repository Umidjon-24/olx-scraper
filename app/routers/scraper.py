from datetime import datetime, timezone

from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import ScrapeRun
from app.crud import get_scrape_runs
from app.scheduler import run_scrape_job, _running

router = APIRouter()


@router.post("/run", summary="Manually trigger a scrape now")
async def trigger_scrape(background_tasks: BackgroundTasks):
    """
    Kicks off a full scrape in the background.
    Returns immediately — check /scraper/runs for progress.
    """
    background_tasks.add_task(run_scrape_job)
    return {"status": "started", "message": "Scrape job submitted. Check /scraper/runs for progress."}


@router.post("/cancel", summary="Mark all stuck 'running' scrape runs as cancelled")
async def cancel_runs(db: AsyncSession = Depends(get_db)):
    """
    Marks all runs with status='running' as 'cancelled'.
    Use this to clean up zombie runs after a redeploy.
    """
    result = await db.execute(
        update(ScrapeRun)
        .where(ScrapeRun.status == "running")
        .values(
            status="cancelled",
            finished_at=datetime.now(timezone.utc),
            error="Manually cancelled via API",
        )
    )
    await db.commit()
    cancelled = result.rowcount
    return {"cancelled": cancelled, "message": f"{cancelled} stuck run(s) marked as cancelled"}


@router.get("/status", summary="Check if a scrape is currently running")
async def scrape_status():
    """Returns whether the scraper is currently active."""
    return {"running": _running}


@router.get("/runs", summary="List recent scrape runs")
async def list_runs(limit: int = 20, db: AsyncSession = Depends(get_db)):
    runs = await get_scrape_runs(db, limit=limit)
    return [
        {
            "id": r.id,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "status": r.status,
            "pages_scraped": r.pages_scraped,
            "listings_found": r.listings_found,
            "listings_saved": r.listings_saved,
            "error": r.error,
        }
        for r in runs
    ]
