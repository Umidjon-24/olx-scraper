from datetime import datetime
from sqlalchemy import (
    Integer, String, Boolean, DateTime, Text, BigInteger, func
)
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class Listing(Base):
    """One OLX real-estate listing."""
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # OLX identifiers
    listing_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    url: Mapped[str | None] = mapped_column(Text)

    # Core fields
    title: Mapped[str | None] = mapped_column(Text)
    price: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(10))
    area: Mapped[str | None] = mapped_column(String(64))
    num_rooms: Mapped[int | None] = mapped_column(Integer)
    market_type: Mapped[str | None] = mapped_column(String(128))
    stair: Mapped[str | None] = mapped_column(String(64))
    negotiation: Mapped[bool] = mapped_column(Boolean, default=False)

    # Metadata
    views: Mapped[int | None] = mapped_column(Integer)
    posted_date: Mapped[str | None] = mapped_column(String(128))
    seller: Mapped[str | None] = mapped_column(String(256))
    location: Mapped[str | None] = mapped_column(Text)
    seller_joined: Mapped[str | None] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)

    # Housekeeping
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScrapeRun(Base):
    """Audit log of every scrape job execution."""
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")  # running | success | failed
    pages_scraped: Mapped[int] = mapped_column(Integer, default=0)
    listings_found: Mapped[int] = mapped_column(Integer, default=0)
    listings_saved: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
