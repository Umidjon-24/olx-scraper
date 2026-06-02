# ── Base ────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System deps for Playwright Chromium ─────────────────────────
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    fonts-unifont fonts-liberation \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# ── Playwright browsers path (must match what playwright install uses) ──
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# ── Working directory ────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ──────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Install Playwright + Chromium (same as Colab's !playwright install chromium) ──
RUN playwright install chromium
RUN playwright install-deps chromium

# ── Copy source ──────────────────────────────────────────────────
COPY . .

# ── Run the scheduler ────────────────────────────────────────────
CMD ["python", "start.py"]
