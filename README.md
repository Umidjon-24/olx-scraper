# OLX Scraper — FastAPI + PostgreSQL

Scrapes OLX.uz apartment listings every day and stores them in PostgreSQL.
Built with **FastAPI**, **SQLAlchemy (async)**, **Playwright**, and **APScheduler**.

---

## Project structure

```
olx-scraper/
├── app/
│   ├── main.py          # FastAPI app + lifespan
│   ├── config.py        # Settings (env vars)
│   ├── database.py      # Async SQLAlchemy engine
│   ├── models.py        # listings + scrape_runs tables
│   ├── crud.py          # DB queries / upsert
│   ├── scraper.py       # Playwright scraping logic
│   ├── scheduler.py     # APScheduler daily cron
│   └── routers/
│       ├── scraper.py   # POST /scraper/run, GET /scraper/runs
│       └── listings.py  # GET /listings/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── start.py             # Init DB then launch uvicorn
└── .env.example
```

---

## Quick start (Docker — recommended)

```bash
# 1. Copy and edit environment file
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD, etc.

# 2. Build and start everything
docker compose up --build

# API is live at http://localhost:8000
# Swagger docs at http://localhost:8000/docs
```

---

## Quick start (local / venv)

```bash
# 1. Python 3.12+
python -m venv .venv && source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt
playwright install chromium && playwright install-deps chromium

# 3. Start PostgreSQL (or point .env at an existing server)

# 4. Copy env and edit
cp .env.example .env

# 5. Run (creates tables then starts server)
python start.py
```

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health check |
| `POST` | `/scraper/run` | Trigger a scrape immediately |
| `GET` | `/scraper/runs` | List recent scrape jobs + stats |
| `GET` | `/listings/` | Query stored listings |

### Listings query params

| Param | Type | Example |
|-------|------|---------|
| `skip` | int | `0` |
| `limit` | int | `50` (max 500) |
| `min_price` | int | `100000` |
| `max_price` | int | `500000` |
| `num_rooms` | int | `2` |
| `currency` | str | `USD` or `UZS` |

**Example:**
```
GET /listings/?num_rooms=2&currency=USD&max_price=150000&limit=20
```

---

## Configuration (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_USER` | `olx_user` | DB username |
| `POSTGRES_PASSWORD` | `olx_pass` | DB password |
| `POSTGRES_HOST` | `localhost` | DB host |
| `POSTGRES_PORT` | `5432` | DB port |
| `POSTGRES_DB` | `olx_db` | Database name |
| `OLX_BASE_URL` | apartment listings URL | What to scrape |
| `MAX_PAGES` | `5` | How many listing pages per run |
| `SCRAPE_HOUR` | `2` | Hour to run daily (Tashkent time) |
| `SCRAPE_MINUTE` | `0` | Minute to run daily |

---

## Database tables

### `listings`
Stores one row per unique OLX listing (upserted on `listing_id`).
Fields: `listing_id`, `title`, `price`, `currency`, `area`, `num_rooms`,
`market_type`, `stair`, `negotiation`, `views`, `posted_date`, `seller`,
`location`, `seller_joined`, `description`, `url`, `scraped_at`, `updated_at`.

### `scrape_runs`
Audit log of every scrape execution.
Fields: `started_at`, `finished_at`, `status`, `pages_scraped`,
`listings_found`, `listings_saved`, `error`.

---

## Changing the schedule

Edit `.env`:
```
SCRAPE_HOUR=6
SCRAPE_MINUTE=30
```
This runs every day at **06:30 Tashkent time**.

---

## Changing what is scraped

Edit `OLX_BASE_URL` in `.env` to any OLX category URL, e.g.:

```
# Houses for sale
OLX_BASE_URL=https://www.olx.uz/nedvizhimost/doma-dachi/prodazha/?currency=UZS

# Apartments for rent
OLX_BASE_URL=https://www.olx.uz/nedvizhimost/kvartiry/dolgosrochnaya-arenda/?currency=UZS
```
