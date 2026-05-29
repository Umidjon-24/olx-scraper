from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.scheduler import start_scheduler, stop_scheduler
from app.routers import scraper, listings


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(
    title="OLX Scraper API",
    description="Scrapes OLX.uz real estate listings daily and stores them in PostgreSQL.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(scraper.router, prefix="/scraper", tags=["Scraper"])
app.include_router(listings.router, prefix="/listings", tags=["Listings"])


@app.get("/")
def root():
    return {"status": "ok", "message": "OLX Scraper API is running"}
