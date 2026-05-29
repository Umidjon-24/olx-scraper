from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.crud import get_listings

router = APIRouter()


@router.get("/", summary="Query stored listings")
async def list_listings(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    min_price: int | None = Query(None, description="Minimum price"),
    max_price: int | None = Query(None, description="Maximum price"),
    num_rooms: int | None = Query(None, description="Filter by number of rooms"),
    currency: str | None = Query(None, description="USD or UZS"),
    db: AsyncSession = Depends(get_db),
):
    listings = await get_listings(
        db,
        skip=skip,
        limit=limit,
        min_price=min_price,
        max_price=max_price,
        num_rooms=num_rooms,
        currency=currency,
    )
    return [
        {
            "id": l.id,
            "listing_id": l.listing_id,
            "title": l.title,
            "price": l.price,
            "currency": l.currency,
            "area": l.area,
            "num_rooms": l.num_rooms,
            "market_type": l.market_type,
            "stair": l.stair,
            "negotiation": l.negotiation,
            "views": l.views,
            "posted_date": l.posted_date,
            "seller": l.seller,
            "location": l.location,
            "seller_joined": l.seller_joined,
            "description": l.description,
            "url": l.url,
            "scraped_at": l.scraped_at,
        }
        for l in listings
    ]
