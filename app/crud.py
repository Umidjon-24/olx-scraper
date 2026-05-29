from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Listing, ScrapeRun


# ─────────────────────────────────────────────────────────────────
# SCRAPE RUN
# ─────────────────────────────────────────────────────────────────

async def create_scrape_run(db: AsyncSession) -> ScrapeRun:
    run = ScrapeRun(status="running")
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def finish_scrape_run(
    db: AsyncSession,
    run_id: int,
    *,
    status: str,
    pages_scraped: int = 0,
    listings_found: int = 0,
    listings_saved: int = 0,
    error: str | None = None,
) -> None:
    await db.execute(
        update(ScrapeRun)
        .where(ScrapeRun.id == run_id)
        .values(
            finished_at=datetime.now(timezone.utc),
            status=status,
            pages_scraped=pages_scraped,
            listings_found=listings_found,
            listings_saved=listings_saved,
            error=error,
        )
    )
    await db.commit()


# ─────────────────────────────────────────────────────────────────
# LISTINGS — upsert (insert or update on listing_id conflict)
# ─────────────────────────────────────────────────────────────────

async def upsert_listings(db: AsyncSession, rows: list[dict]) -> int:
    if not rows:
        return 0

    saved = 0
    for row in rows:
        listing_id = row.get("listing_id")

        if listing_id:
            stmt = (
                insert(Listing)
                .values(**row)
                .on_conflict_do_update(
                    index_elements=["listing_id"],
                    set_={
                        k: row[k]
                        for k in row
                        if k not in ("listing_id", "scraped_at")
                    },
                )
            )
        else:
            # No unique ID — just insert
            stmt = insert(Listing).values(**row)

        await db.execute(stmt)
        saved += 1

    await db.commit()
    return saved


# ─────────────────────────────────────────────────────────────────
# LISTINGS — queries
# ─────────────────────────────────────────────────────────────────

async def get_listings(
    db: AsyncSession,
    skip: int = 0,
    limit: int = 50,
    min_price: int | None = None,
    max_price: int | None = None,
    num_rooms: int | None = None,
    currency: str | None = None,
) -> list[Listing]:
    q = select(Listing).order_by(Listing.scraped_at.desc())

    if min_price is not None:
        q = q.where(Listing.price >= min_price)
    if max_price is not None:
        q = q.where(Listing.price <= max_price)
    if num_rooms is not None:
        q = q.where(Listing.num_rooms == num_rooms)
    if currency is not None:
        q = q.where(Listing.currency == currency.upper())

    q = q.offset(skip).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_scrape_runs(db: AsyncSession, limit: int = 20) -> list[ScrapeRun]:
    q = select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())
