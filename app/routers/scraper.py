from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.crud import get_scrape_runs
from app.scheduler import run_scrape_job

router = APIRouter()


@router.post("/run", summary="Manually trigger a scrape now")
async def trigger_scrape(background_tasks: BackgroundTasks):
    """
    Kicks off a full scrape in the background.
    Returns immediately — check /scraper/runs for status.
    """
    background_tasks.add_task(run_scrape_job)
    return {"status": "started", "message": "Scrape job submitted. Check /scraper/runs for progress."}


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
