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


if __name__ == "__main__":
    asyncio.run(init())
    sys.exit(
        subprocess.call([
            "uvicorn", "app.main:app",
            "--host", "0.0.0.0",
            "--port", "8000",
            "--reload",
        ])
    )
