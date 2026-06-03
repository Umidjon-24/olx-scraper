#!/usr/bin/env python3
import asyncio
from app.scraper import run_scrape

if __name__ == "__main__":
    asyncio.run(run_scrape())
