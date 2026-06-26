FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Reinstall Chromium for whatever Playwright version pip resolved, so the
# bundled browser can never mismatch the library again.
RUN playwright install chromium
COPY . .
CMD ["python", "olx_scraper.py"]
