import asyncio
import time
import os
import schedule
from datetime import datetime

from app.scraper import run_scrape

def job():
    print(f"[SCHEDULER] Triggering scrape at {datetime.now()}")
    asyncio.run(run_scrape())

# ── Read time from Railway Variables ─────────────────────────────
# SCRAPE_HOUR and SCRAPE_MINUTE are Tashkent time (UTC+5)
# Convert to UTC by subtracting 5 hours
scrape_hour_tashkent   = int(os.environ.get("SCRAPE_HOUR", "10"))
scrape_minute_tashkent = int(os.environ.get("SCRAPE_MINUTE", "15"))

utc_hour   = (scrape_hour_tashkent - 5) % 24
utc_minute = scrape_minute_tashkent

run_at = f"{utc_hour:02d}:{utc_minute:02d}"

print(f"[SCHEDULER] Scrape scheduled at {scrape_hour_tashkent:02d}:{scrape_minute_tashkent:02d} Tashkent = {run_at} UTC")
print(f"[SCHEDULER] Current time: {datetime.now()}")

schedule.every().day.at(run_at).do(job)

while True:
    schedule.run_pending()
    time.sleep(60)
