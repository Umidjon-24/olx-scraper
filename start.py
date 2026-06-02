#!/usr/bin/env python3
"""
Start-up helper:
1. Creates all DB tables (idempotent)
2. Launches uvicorn
"""
import asyncio
import subprocess
import sys


async def init():
    from app.database import init_db
    print("Creating database tables...")
    await init_db()
    print("Tables ready.")


"""
start.py — Railway entry point.
Runs the scheduler which triggers the scraper daily.
To trigger an immediate manual scrape, run: python -m app.scraper
"""

if __name__ == "__main__":
    subprocess.run([sys.executable, "scheduler.py"])
