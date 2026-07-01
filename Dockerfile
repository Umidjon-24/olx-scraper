FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

# tini becomes PID 1 and reaps orphaned/zombie child processes at the OS level.
# This is the root-cause fix for the ~600 <defunct> Chromium processes seen in
# the logs: without an init, orphaned browser children reparent to the Python
# app (PID 1), which never wait()s them, so they pile up and starve the
# container — which is what caused the "Page rendered incomplete" cascade.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Reinstall Chromium for whatever Playwright version pip resolved, so the
# bundled browser can never mismatch the library again.
RUN playwright install chromium

COPY . .

# Run the app under tini so signals are forwarded and dead children are reaped.
ENTRYPOINT ["tini", "--"]
CMD ["python", "olx_scraper.py"]
