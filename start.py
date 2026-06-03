#!/usr/bin/env python3
"""
Start-up helper:
Launches the scraper as a background process.
"""
import subprocess
import sys

if __name__ == "__main__":
    subprocess.run([sys.executable, "-u", "-m", "app.scraper"])


