"""
scheduler.py — Daily cron job for OLX scraper.
Runs run_scrape() every day at 02:00 Tashkent time.
The infinite loop keeps the Railway container alive.
"""

import asyncio
import time
import schedule
from datetime import datetime

from app.scraper import run_scrape


def job():
    print(f"[SCHEDULER] Triggering scrape at {datetime.now()}")
    asyncio.run(run_scrape())


# ── Schedule: every day at 02:00 Tashkent time ─────────────────
# Railway containers run UTC. Tashkent is UTC+5, so 05:15 = 10:15 UTC previous day.
schedule.every().day.at("05:15").do(job)   # 05:15 UTC = 10:15 Tashkent

print("[SCHEDULER] Scheduler started. Waiting for next run at 05:15 UTC (10:15 Tashkent)...")
print(f"[SCHEDULER] Current time: {datetime.now()}")

while True:
    schedule.run_pending()
    time.sleep(60)
