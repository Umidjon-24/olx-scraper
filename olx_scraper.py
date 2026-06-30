"""
OLX scraper — Railway production version.
Scrapes apartment listings from olx.uz and upserts into Supabase.

Data sources, in priority order:
  1. <script type="application/ld+json">  — server-rendered, reliable title/price/currency/description.
  2. The on-page parameter list (Общая площадь, Этаж, …) parsed element-by-element.
  3. DOM selectors / description inference as a last resort.

────────────────────────────────────────────────────────────────────────────
CHANGES in this version:
  • RESTORED the CONFIG block (BASE_URL, MAX_PAGES, DB_*, TABLE_NAME). All still
    read from env vars exactly as before — defaults shown below.
  • SINGLE-TRIGGER SCHEDULER at the bottom (see ENTRYPOINT). Honors RUN_ON_START
    and SCRAPE_HOUR, but the boot run is awaited in the SAME event loop as the
    cron, so the in-process _scrape_lock actually protects against overlap.
    APScheduler's max_instances=1 is a second backstop.
  • Postgres ADVISORY LOCK (try_acquire_run_lock): a cross-process/replica mutex.
    A second run — from a deploy handoff, a stray replica, or anything the
    in-process lock can't see — bails immediately without touching the first.
  • Memory/speed fixes retained: flush_page() between ads, kill_orphan_chrome()
    on restart, extended request blocking, wait_for_param() instead of a blind
    sleep, breadcrumb-first location, lower slow_mo / fewer scroll passes.
  • ensure_table migrates ANY pre-existing table to the full schema before
    referencing columns (fixes 'column scraped_date does not exist').
────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import json
import math
import random
import re
import os
import signal
import time
from datetime import datetime

import pandas as pd
from playwright.async_api import async_playwright
from sqlalchemy import create_engine, text

# Optional: precise orphan-Chromium reaping. If psutil isn't installed we just
# skip the sweep (add `psutil` to requirements.txt to enable it on Railway).
try:
    import psutil
    _HAVE_PSUTIL = True
except Exception:
    _HAVE_PSUTIL = False


# ─────────────────────────────────────────────────────────────────
# CONFIG  — all read from environment variables (set these in Railway).
# ─────────────────────────────────────────────────────────────────
BASE_URL   = os.getenv("OLX_BASE_URL", "https://www.olx.uz/nedvizhimost/kvartiry/prodazha/?currency=UZS")
MAX_PAGES  = int(os.getenv("MAX_PAGES", "25"))

DB_USER    = os.environ.get("DB_USER")
DB_PASS    = os.environ.get("DB_PASS")
DB_HOST    = os.environ.get("DB_HOST", "aws-0-ap-southeast-1.pooler.supabase.com")
DB_PORT    = os.environ.get("DB_PORT", "5432")
DB_NAME    = os.environ.get("DB_NAME", "postgres")
TABLE_NAME = os.getenv("TABLE_NAME", "olx_listings")

# How many of the first ads to dump full diagnostics for (set DIAG_DUMP>0 to enable).
DIAG_DUMP  = int(os.getenv("DIAG_DUMP", "0"))

# Resume: within a run, skip listings that already have TODAY's snapshot row.
RESUME_SKIP_DONE_TODAY = os.getenv("RESUME_SKIP_DONE_TODAY", "true").lower() == "true"

# Hard wall-clock budget for a single run.
MAX_RUNTIME_HOURS = float(os.getenv("MAX_RUNTIME_HOURS", "22"))

# ─────────────────────────────────────────────────────────────────
# TIMING
# ─────────────────────────────────────────────────────────────────
AD_WAIT_MS            = (600, 1200)   # small settle once content is already ready
# Readiness budgets (the slow part used to be three blind sequential waits).
READY_WAIT_MS         = int(os.getenv("READY_WAIT_MS", "15000"))   # overall ceiling to get usable content
CHALLENGE_WAIT_MS     = int(os.getenv("CHALLENGE_WAIT_MS", "12000"))  # let a Cloudflare interstitial self-resolve
PARAM_WAIT_MS         = int(os.getenv("PARAM_WAIT_MS", "3500"))    # bounded; many valid ads lack 'Общая площадь'
BETWEEN_ADS           = (2.0, 4.0)    # ← the real rate limiter; raise this first if 403s rise
BETWEEN_LIST          = (4.5, 9.0)
LONG_BREAK_EVERY      = 22
LONG_BREAK_SECS       = (13, 22)
SCROLL_PASSES         = (1, 2)
SCROLL_DIST_PX        = (500, 1200)
SCROLL_PAUSE          = (0.3, 0.6)
BROWSER_RESTART_EVERY = int(os.getenv("BROWSER_RESTART_EVERY", "15"))
AD_ATTEMPTS           = int(os.getenv("AD_ATTEMPTS", "4"))
BATCH_SIZE            = 1             # save every listing immediately

# Unique key for the Postgres advisory lock (any constant 32-bit int works).
RUN_LOCK_KEY = int(os.getenv("RUN_LOCK_KEY", "916273"))

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disk-cache-size=1",
    "--media-cache-size=1",
    "--disable-application-cache",
    "--disable-gpu",
    "--disable-accelerated-2d-canvas",
    "--disable-web-security",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--hide-scrollbars",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-first-run",
    "--safebrowsing-disable-auto-update",
    # NOTE: heap-capping flags (--single-process, --no-zygote,
    # --js-flags=--max-old-space-size) are intentionally absent — they break
    # React rendering on heavier pages and cause null fields.
]


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

def clean(text):
    if not text:
        return None
    return re.sub(r"\s+", " ", str(text)).strip() or None


def extract_number(text):
    if not text:
        return None
    text = str(text).replace(" ", "").replace(" ", "")
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def listing_id_from_url(url):
    """OLX listing id embedded in the URL, e.g. '…-IDabc123.html' → 'IDabc123'."""
    m = re.search(r"-(ID[A-Za-z0-9]+)\.html", url or "")
    return m.group(1) if m else None


def normalize_area(val):
    """Normalize an area value to '<number> м²'."""
    if not val:
        return None
    m = re.search(r"(\d+[.,]?\d*)", str(val))
    if not m:
        return clean(val)
    return f"{m.group(1).replace(',', '.')} м²"


def clean_location(raw):
    """Turn the raw map-block text into a clean 'City, District' (drops widget junk)."""
    if not raw:
        return None
    t = clean(raw)
    if not t:
        return None
    t = re.split(
        r"\s*(?:Изображени|Картограф|Посмотрет|Услови|Show|Map data|Terms|©|http)",
        t, maxsplit=1, flags=re.I,
    )[0]
    t = re.sub(r"\b(Продажа|Аренда|Sotuv|Ijara)\s*[-–]\s*", "", t, flags=re.I)
    t = clean(t)
    if not t:
        return None
    m = re.search(r"^(.*?\bрайон)\b", t, flags=re.I)
    if m:
        return clean(m.group(1))
    return clean(t[:60])


def location_from_title(page_title):
    """OLX titles end with '… - Продажа <City> на Olx' — pull the city out."""
    if not page_title:
        return None
    m = re.search(r".*Продажа\s+(.+?)\s+на\s+Olx\s*$", page_title, re.I)
    return clean(m.group(1)) if m else None


async def short_delay(a, b):
    await asyncio.sleep(random.uniform(a, b))


async def human_scroll(page, fast=False):
    passes = 1 if fast else random.randint(*SCROLL_PASSES)
    for _ in range(passes):
        dist = random.randint(*SCROLL_DIST_PX)
        await page.mouse.wheel(0, dist)
        await asyncio.sleep(random.uniform(*SCROLL_PAUSE))
        if not fast and random.random() < 0.3:
            await asyncio.sleep(random.uniform(0.5, 1.2))


async def flush_page(page):
    """Navigate to about:blank so the renderer can GC the previous SPA heap."""
    try:
        await page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
    except Exception:
        pass


def kill_orphan_chrome():
    """SIGKILL leftover Chromium processes. Safe only between browsers."""
    if not _HAVE_PSUTIL:
        return
    killed = 0
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if "chrome" in name or "chromium" in name:
            try:
                proc.send_signal(signal.SIGKILL)
                killed += 1
            except Exception:
                pass
    if killed:
        print(f"  [mem] reaped {killed} orphan Chromium process(es)")


# ─────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────

COLUMNS = [
    "listing_id", "snapshot_date", "olx_id", "title", "price", "currency", "area",
    "num_rooms", "market_type", "views", "stair", "total_floors", "posted_date",
    "scraped_date", "negotiation", "seller", "location", "seller_joined",
    "description", "url",
]
UPDATE_COLUMNS = [c for c in COLUMNS if c not in ("listing_id", "snapshot_date")]


def get_engine():
    if not DB_USER or not DB_PASS:
        raise RuntimeError("DB_USER and DB_PASS environment variables are required.")
    from sqlalchemy.engine import URL
    url = URL.create(
        drivername="postgresql+psycopg2",
        username=DB_USER,
        password=DB_PASS,
        host=DB_HOST,
        port=int(DB_PORT),
        database=DB_NAME,
    )
    return create_engine(url, pool_pre_ping=True)


def try_acquire_run_lock(engine):
    """Cross-process mutex via a Postgres advisory lock.

    Returns an OPEN connection if we got the lock, None if another run holds it,
    or the sentinel 'no-lock' if the lock check itself errored (we then proceed
    unprotected rather than fail the run). Auto-releases if the connection drops.
    """
    conn = engine.connect()
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": RUN_LOCK_KEY}).scalar()
    except Exception as e:
        conn.close()
        print(f"  [lock] advisory-lock check failed (continuing without it): {e}")
        return "no-lock"
    if not got:
        conn.close()
        return None
    return conn


def release_run_lock(lock_conn):
    if lock_conn in (None, "no-lock"):
        return
    try:
        lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": RUN_LOCK_KEY})
    except Exception:
        pass
    try:
        lock_conn.close()
    except Exception:
        pass


def ensure_table(engine):
    """Create the listings table if needed and migrate the schema in place."""
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                listing_id    TEXT NOT NULL,
                snapshot_date DATE NOT NULL DEFAULT CURRENT_DATE,
                olx_id        BIGINT,
                title         TEXT,
                price         NUMERIC,
                currency      TEXT,
                area          TEXT,
                num_rooms     INT,
                market_type   TEXT,
                views         INT,
                stair         TEXT,
                total_floors  TEXT,
                posted_date   TEXT,
                scraped_date  TEXT,
                negotiation   BOOLEAN,
                seller        TEXT,
                location      TEXT,
                seller_joined TEXT,
                description   TEXT,
                url           TEXT,
                PRIMARY KEY (listing_id, snapshot_date)
            )
        """))
        # Bring ANY pre-existing table up to the current schema BEFORE referencing
        # these columns below.
        schema_types = {
            "olx_id": "BIGINT", "title": "TEXT", "price": "NUMERIC", "currency": "TEXT",
            "area": "TEXT", "num_rooms": "INT", "market_type": "TEXT", "views": "INT",
            "stair": "TEXT", "total_floors": "TEXT", "posted_date": "TEXT",
            "scraped_date": "TEXT", "negotiation": "BOOLEAN", "seller": "TEXT",
            "location": "TEXT", "seller_joined": "TEXT", "description": "TEXT", "url": "TEXT",
        }
        for col, col_type in schema_types.items():
            conn.execute(text(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {col} {col_type}"))

        conn.execute(text(f"ALTER TABLE {TABLE_NAME} DROP COLUMN IF EXISTS first_seen"))
        conn.execute(text(f"ALTER TABLE {TABLE_NAME} DROP COLUMN IF EXISTS last_seen"))
        conn.execute(text(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS snapshot_date DATE"))
        conn.execute(text(
            f"UPDATE {TABLE_NAME} SET snapshot_date = "
            f"COALESCE(NULLIF(left(scraped_date, 10), '')::date, CURRENT_DATE) "
            f"WHERE snapshot_date IS NULL"
        ))
        conn.execute(text(f"ALTER TABLE {TABLE_NAME} ALTER COLUMN snapshot_date SET NOT NULL"))
        conn.execute(text(f"ALTER TABLE {TABLE_NAME} ALTER COLUMN snapshot_date SET DEFAULT CURRENT_DATE"))
        conn.execute(text(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_attribute a
                      ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
                    WHERE c.conrelid = '{TABLE_NAME}'::regclass
                      AND c.contype = 'p' AND a.attname = 'snapshot_date'
                ) THEN
                    ALTER TABLE {TABLE_NAME} DROP CONSTRAINT IF EXISTS {TABLE_NAME}_pkey;
                    ALTER TABLE {TABLE_NAME} ADD CONSTRAINT {TABLE_NAME}_pkey
                        PRIMARY KEY (listing_id, snapshot_date);
                END IF;
            END $$;
        """))
    print(f"  [DB] Table '{TABLE_NAME}' ready (daily-snapshot mode: PK = listing_id + snapshot_date).")


def load_done_today(engine):
    """listing_ids that already have a snapshot row for today — skipped on resume."""
    if not RESUME_SKIP_DONE_TODAY:
        return set()
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text(f"SELECT listing_id FROM {TABLE_NAME} WHERE snapshot_date = CURRENT_DATE")
            ).fetchall()
    except Exception as e:
        print(f"  [resume] could not load today's snapshot ids (continuing full scrape): {e}")
        return set()
    return {r[0] for r in rows if r[0]}


def tidy_existing_location(loc):
    c = clean_location(loc)
    if not c:
        return None
    parts = [p.strip() for p in c.split(",")]
    if len(parts) >= 3 and "область" in parts[0].lower():
        c = ", ".join(parts[1:])
    return c


def backfill_locations(engine):
    if os.getenv("BACKFILL_LOCATIONS", "false").lower() != "true":
        return
    print("  [backfill] cleaning existing location values...")
    with engine.begin() as conn:
        rows = conn.execute(
            text(f"SELECT listing_id, location FROM {TABLE_NAME} WHERE location IS NOT NULL")
        ).fetchall()
    fixed = 0
    for lid, loc in rows:
        new = tidy_existing_location(loc)
        if new and new != loc:
            with engine.begin() as conn:
                conn.execute(
                    text(f"UPDATE {TABLE_NAME} SET location = :l WHERE listing_id = :id"),
                    {"l": new, "id": lid},
                )
            fixed += 1
    print(f"  [backfill] updated {fixed}/{len(rows)} location values.")


def save_batch_to_db(data_list, engine):
    if not data_list:
        return
    df = pd.DataFrame(data_list)
    df = df[[c for c in COLUMNS if c in df.columns]]
    records = df.to_dict(orient="records")
    for rec in records:
        for k, v in rec.items():
            if isinstance(v, float) and math.isnan(v):
                rec[k] = None

    saved = 0
    failed = 0
    for rec in records:
        if not rec.get("listing_id"):
            continue
        cols    = [c for c in rec.keys()]
        values  = [f":{c}" for c in cols]
        updates = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in UPDATE_COLUMNS if c in cols
        )
        sql = text(f"""
            INSERT INTO {TABLE_NAME} ({", ".join(cols)})
            VALUES ({", ".join(values)})
            ON CONFLICT (listing_id, snapshot_date) DO UPDATE SET
                {updates}
        """)
        try:
            with engine.begin() as conn:
                conn.execute(sql, rec)
            saved += 1
        except Exception as e:
            failed += 1
            print(f"  [DB] ✗ Failed to save listing {rec.get('listing_id')}: {e}")
    print(f"  [DB] ✓ {saved} saved, {failed} failed.")


# ─────────────────────────────────────────────────────────────────
# BROWSER FACTORY
# ─────────────────────────────────────────────────────────────────

BLOCKED_RESOURCES = {"image", "media", "font"}
BLOCKED_URL_FRAGMENTS = (
    "maps.googleapis.com", "maps.gstatic.com", "google-analytics.com",
    "googletagmanager.com", "doubleclick.net", "googlesyndication.com",
    "facebook.net", "hotjar.com", "/tile", "/tiles/",
)


async def make_browser_page(p):
    browser = await p.chromium.launch(headless=True, slow_mo=20, args=CHROMIUM_ARGS)
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="ru-RU",
        timezone_id="Asia/Tashkent",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()

    async def block_resources(route):
        req = route.request
        url = req.url
        if req.resource_type in BLOCKED_RESOURCES:
            await route.abort()
            return
        if any(frag in url for frag in BLOCKED_URL_FRAGMENTS):
            await route.abort()
            return
        await route.continue_()
    await page.route("**/*", block_resources)

    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU','ru','en-US'] });
    """)
    return browser, page


async def restart_browser(p, browser):
    try:
        await browser.close()
    except Exception:
        pass
    await asyncio.sleep(2)
    kill_orphan_chrome()
    await asyncio.sleep(3)
    return await make_browser_page(p)


# ─────────────────────────────────────────────────────────────────
# LINK COLLECTION
# ─────────────────────────────────────────────────────────────────

def page_url(base, page_num):
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}page={page_num}" if page_num > 1 else base


async def get_all_links(p, base_url, max_pages):
    all_links = set()
    browser, page = await make_browser_page(p)
    empty_streak = 0
    nogain_streak = 0

    for pg in range(1, max_pages + 1):
        url = page_url(base_url, pg)
        print(f"── LIST PAGE {pg}/{max_pages}: {url}")
        page_raw = 0
        page_gained = 0

        for attempt in range(3):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(random.randint(3000, 5000))
                try:
                    await page.wait_for_selector("a[href*='/d/obyavlenie/']", timeout=15000)
                except Exception:
                    pass
                await human_scroll(page)
            except Exception as e:
                print(f"   ✗ Error (attempt {attempt+1}/3): {e}")
                browser, page = await restart_browser(p, browser)
                await asyncio.sleep(5)
                continue

            page_title = (await page.title()).lower()
            body_text  = (await page.text_content("body") or "").lower()

            if "403" in page_title or "access denied" in body_text or "captcha" in body_text:
                wait_secs = (attempt + 1) * 15
                print(f"   ✗ 403 — waiting {wait_secs}s (attempt {attempt+1}/3)")
                await asyncio.sleep(wait_secs)
                continue

            hrefs = await page.locator("a").evaluate_all("els => els.map(e => e.href)")
            raw_links = [h.split("?")[0] for h in hrefs if h and "/d/obyavlenie/" in h]
            before = len(all_links)
            all_links.update(raw_links)
            gained = len(all_links) - before
            page_raw = len(raw_links)
            page_gained = gained
            print(f"   +{gained} new  (total {len(all_links)})")

            if gained == 0 and pg > 1:
                if raw_links:
                    print(f"   ~ all {len(raw_links)} links already seen (feed shifted) — moving on")
                    break
                wait_secs = (attempt + 1) * 20
                print(f"   ✗ Empty page — possible block, waiting {wait_secs}s before retry")
                await asyncio.sleep(wait_secs)
                continue
            break
        else:
            print(f"   ✗ Skipping page {pg} after 3 failed attempts")

        empty_streak  = empty_streak + 1  if page_raw == 0    else 0
        nogain_streak = nogain_streak + 1 if page_gained == 0 else 0

        if pg > 1 and empty_streak >= 2:
            print(f"\n  ✓ Reached end of results — {empty_streak} empty pages. Stopping at page {pg}.")
            break
        if pg > 1 and nogain_streak >= 4:
            print(f"\n  ✓ No new listings for {nogain_streak} pages — assuming end. Stopping at page {pg}.")
            break

        if pg < max_pages:
            await short_delay(*BETWEEN_LIST)

    try:
        await browser.close()
    except Exception:
        pass
    kill_orphan_chrome()
    print(f"  Link collection finished: {len(all_links)} unique listings.")
    return list(all_links)


# ─────────────────────────────────────────────────────────────────
# JSON-LD EXTRACTOR
# ─────────────────────────────────────────────────────────────────

def _iter_jsonld_objects(parsed):
    stack = [parsed]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            if "@graph" in node and isinstance(node["@graph"], list):
                stack.extend(node["@graph"])
        elif isinstance(node, list):
            stack.extend(node)


async def extract_from_jsonld(page):
    result = {}
    try:
        blobs = await page.evaluate(
            """() => Array.from(
                document.querySelectorAll('script[type="application/ld+json"]')
            ).map(s => s.textContent || '')"""
        )
    except Exception as e:
        print(f"  [json-ld] eval error: {e}")
        return result

    for raw in blobs:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            continue

        for obj in _iter_jsonld_objects(parsed):
            if not result.get("title") and obj.get("name"):
                result["title"] = clean(obj.get("name"))
            if not result.get("description") and obj.get("description"):
                result["description"] = clean(obj.get("description"))

            offers = obj.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict):
                if not result.get("price"):
                    pr = offers.get("price") or offers.get("lowPrice")
                    if pr not in (None, "", 0, "0"):
                        result["price"] = extract_number(pr)
                if not result.get("currency"):
                    cur = (offers.get("priceCurrency") or "").upper()
                    if cur:
                        result["currency"] = (
                            "USD" if cur in ("USD", "U.E.", "UE") else
                            "UZS" if cur in ("UZS", "SUM", "СУМ") else cur
                        )
                avail = str(offers.get("availability") or "").lower()
                if "soldout" in avail or "discontinued" in avail:
                    result["_sold"] = True

            addr = obj.get("address")
            if not result.get("location") and addr:
                if isinstance(addr, dict):
                    parts = [clean(addr.get(k)) for k in
                             ("streetAddress", "addressLocality", "addressRegion")]
                    parts = [x for x in parts if x]
                    if parts:
                        result["location"] = ", ".join(dict.fromkeys(parts))
                elif isinstance(addr, str):
                    result["location"] = clean(addr)

            for dk in ("datePosted", "datePublished", "validFrom", "uploadDate"):
                if not result.get("posted_date") and obj.get(dk):
                    result["posted_date"] = clean(str(obj.get(dk)))

            if not result.get("area"):
                fs = obj.get("floorSize")
                if isinstance(fs, dict) and fs.get("value"):
                    unit = fs.get("unitText") or "м²"
                    result["area"] = f"{clean(fs.get('value'))} {unit}".strip()

    return {k: v for k, v in result.items() if v is not None}


# ─────────────────────────────────────────────────────────────────
# PARAMETER LIST EXTRACTOR
# ─────────────────────────────────────────────────────────────────

PARAM_LABELS = [
    "Общая площадь", "Жилая площадь", "Площадь кухни", "Количество комнат",
    "Этажность дома", "Этажей в доме", "Этаж", "Тип жилья", "Тип дома",
    "Ремонт", "Меблирована", "Комиссия", "Новостройка",
]

_PARAM_JS = """
(labels) => {
    const out = {};
    const els = Array.from(document.querySelectorAll('p, li, span, div, dd'));
    for (const lbl of labels) {
        let best = null;
        for (const el of els) {
            if (el.children.length > 3) continue;
            const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!t.startsWith(lbl)) continue;
            const rest = t.slice(lbl.length);
            if (/^[А-Яа-яЁё]/.test(rest)) continue;
            if (best === null || t.length < best.length) best = t;
        }
        if (best !== null) out[lbl] = best;
    }
    return out;
}
"""


async def wait_for_param(page):
    try:
        await page.wait_for_function(
            "() => (document.body && document.body.innerText || '').includes('Общая площадь')",
            timeout=PARAM_WAIT_MS,
        )
        return True
    except Exception:
        return False


async def get_params(page):
    try:
        raw = await page.evaluate(_PARAM_JS, PARAM_LABELS)
    except Exception as e:
        print(f"  [params] eval error: {e}")
        return {}
    params = {}
    for lbl, full in (raw or {}).items():
        val = clean(str(full)[len(lbl):].lstrip(" : \t"))
        if val:
            params[lbl] = val
    return params


# ─────────────────────────────────────────────────────────────────
# LOCATION EXTRACTOR
# ─────────────────────────────────────────────────────────────────

_LOCATION_JS = r"""
() => {
    const norm = s => (s || '').replace(/\s+/g, ' ').trim();
    const sels = [
        '[data-testid="map-aside-section"]',
        '[data-testid="qa-static-ad-map"]',
        '[data-testid="ad-location-link"]',
        '[data-cy="ad-location"]',
        '[data-testid="location-link"]',
    ];
    for (const s of sels) {
        const el = document.querySelector(s);
        if (el) {
            const t = norm(el.innerText);
            if (t && t.length < 160) return {src: s, text: t};
        }
    }
    const heads = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,p,span,div'));
    for (const h of heads) {
        const ht = norm(h.textContent);
        if (/^(Карта|Местоположение|Манзил|Joylashuv|Location)$/i.test(ht)) {
            let n = h.nextElementSibling, depth = 0;
            while (n && depth < 5) {
                const t = norm(n.innerText);
                if (t && t.length < 160) return {src: 'heading:' + ht, text: t};
                n = n.nextElementSibling; depth++;
            }
        }
    }
    return null;
}
"""

_BREADCRUMB_JS = r"""
() => {
    const norm = s => (s || '').replace(/\s+/g, ' ').trim();
    let items = [];
    const bc = document.querySelector('[data-testid="breadcrumbs"], nav[aria-label*="readcrumb" i], ol');
    if (bc) items = Array.from(bc.querySelectorAll('li, a')).map(e => norm(e.textContent)).filter(Boolean);
    return items;
}
"""


async def get_location(page):
    try:
        crumbs = await page.evaluate(_BREADCRUMB_JS)
    except Exception:
        crumbs = None
    bc = _parse_breadcrumb_geo(crumbs)
    if bc:
        return bc, "breadcrumb"

    try:
        await page.evaluate("""async () => {
            const step = 700;
            for (let y = 0; y <= document.body.scrollHeight; y += step) {
                window.scrollTo(0, y);
                await new Promise(r => setTimeout(r, 90));
            }
            window.scrollTo(0, document.body.scrollHeight);
        }""")
        await page.wait_for_timeout(700)
    except Exception:
        pass

    try:
        res = await page.evaluate(_LOCATION_JS)
    except Exception as e:
        print(f"  [location] eval error: {e}")
        res = None
    if res and res.get("text"):
        return clean(res.get("text")), res.get("src")
    return None, None


_BC_CATEGORY = {"главная", "недвижимость", "квартиры", "дома", "коммерческая",
                "продажа", "аренда", "olx", "olx.uz", "uy-joy", "kvartiralar"}


def _parse_breadcrumb_geo(crumbs):
    if not crumbs:
        return None
    items = [re.sub(r"^(Продажа|Аренда|Sotuv|Ijara)\s*[-–]\s*", "", c, flags=re.I).strip()
             for c in crumbs]
    deduped = []
    for c in items:
        if c and (not deduped or deduped[-1] != c):
            deduped.append(c)
    items = deduped
    district = next((c for c in items if "район" in c.lower()), None)
    if not district:
        return None
    region = next((c for c in items if "область" in c.lower()), None)
    city = None
    di = items.index(district)
    if di > 0:
        prev = items[di - 1]
        if "область" not in prev.lower() and prev.lower() not in _BC_CATEGORY:
            city = prev
    head = city or region
    return ", ".join(p for p in (head, district) if p)


async def get_olx_id(page):
    try:
        raw = await page.evaluate(r"""() => {
            for (const el of document.querySelectorAll('[data-cy="ad-footer-bar-section"], [data-testid="ad-footer-bar-section"]')) {
                const m = (el.innerText || '').match(/(\d{6,12})/);
                if (m) return m[1];
            }
            const bt = document.body ? document.body.innerText : '';
            const m = bt.match(/(?:ID|№)\s*[:№.]?\s*(\d{6,12})/);
            return m ? m[1] : null;
        }""")
        return int(raw) if raw and str(raw).isdigit() else None
    except Exception as e:
        print(f"  [olx_id] eval error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────
# SCRAPE ONE AD
# ─────────────────────────────────────────────────────────────────

class NotAListing(Exception):
    """Raised when a URL redirects away from an ad (removed/expired) — skip it."""


# Cloudflare/anti-bot interstitials that AUTO-RESOLVE if we just wait with JS on.
# These are NOT hard blocks — the previous code wrongly treated "just a moment"
# as a 403 and slept 60s, then re-navigated, paying the challenge cost twice.
_CHALLENGE_MARKERS = (
    "just a moment", "checking your browser", "checking if the site connection",
    "verifying you are human", "verify you are human", "needs to review the security",
    "проверяем, человек ли", "подождите", "attention required",
    "cf-browser-verification", "challenge-platform", "_cf_chl", "cf_chl",
)
# Genuine dead-ends — no point waiting, back off and retry later (longer cooldown).
_HARD_BLOCK_MARKERS = (
    "403 error", "error 403", "access denied", "доступ запрещ", "forbidden",
)


async def _quick_state(page):
    """Cheap read of title + a small slice of body text for block/challenge detection."""
    try:
        title = (await page.title() or "").lower()
    except Exception:
        title = ""
    try:
        body = (await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 500)"
        ) or "").lower()
    except Exception:
        body = ""
    return title, body


async def _content_ready(page):
    """Readiness signal that can't be faked by an interstitial.

    Returns 'jsonld' if the server-rendered ad JSON-LD is present (only ever
    exists on a real ad page), 'h1' if a non-empty h1 exists, else ''.
    The caller treats a bare 'h1' as ready ONLY when no challenge is showing.
    """
    try:
        return await page.evaluate("""() => {
            if (document.querySelector('script[type="application/ld+json"]')) return 'jsonld';
            const h = document.querySelector('h1');
            return (h && (h.innerText || '').trim().length > 0) ? 'h1' : '';
        }""")
    except Exception:
        return ''


async def wait_for_ready(page):
    """Poll until the ad is usable, transparently sitting through a Cloudflare
    interstitial if one shows up.

    Returns one of:
      'ready'   — JSON-LD / h1 present, safe to extract.
      'blocked' — hard 403 / access-denied; bail fast and back off longer.
      'timeout' — nothing usable within READY_WAIT_MS; caller retries quickly.

    This replaces the old blind sequence (wait_for_selector h1 18s →
    wait_for_param 8s → fixed sleeps → late block check), which burned ~26s+
    BEFORE it even noticed a block.
    """
    deadline = time.monotonic() + READY_WAIT_MS / 1000
    challenge_until = None  # set when we first see an interstitial

    while time.monotonic() < deadline:
        title, body = await _quick_state(page)
        on_challenge = any(m in title or m in body for m in _CHALLENGE_MARKERS)

        signal = await _content_ready(page)
        # JSON-LD only exists on a genuine ad page; an h1 alone is trusted only
        # when no interstitial text is present (the challenge page has its own h1).
        if signal == "jsonld" or (signal == "h1" and not on_challenge):
            return "ready"

        if any(m in title or m in body for m in _HARD_BLOCK_MARKERS):
            return "blocked"

        if on_challenge:
            # First sighting starts a bounded patience window; the JS solver
            # typically clears it in a few seconds. We keep polling _content_ready.
            now = time.monotonic()
            if challenge_until is None:
                challenge_until = now + CHALLENGE_WAIT_MS / 1000
            elif now > challenge_until:
                return "timeout"  # interstitial never cleared — quick retry

        await page.wait_for_timeout(400)

    return "timeout"


async def scrape_ad(page, url, diag=False):
    views_holder = {"value": None}

    async def handle_response(response):
        try:
            if "statistics" in response.url and response.status == 200:
                data = await response.json()
                v = (data.get("data", {}).get("statistics", {})
                         .get("page_views", {}).get("sum"))
                if v is not None:
                    views_holder["value"] = int(v)
        except Exception:
            pass

    page.on("response", handle_response)
    t0 = time.monotonic()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # Detect blocks/challenges FAST and let CF interstitials self-resolve,
        # instead of paying ~26s of blind waits before noticing a problem.
        state = await wait_for_ready(page)
        if state == "blocked":
            raise Exception("BLOCKED_403")
        if state == "timeout":
            # Nothing usable yet — surface as RENDER_FAILED so the caller does a
            # short, cheap retry rather than the old 30/60/90s block backoff.
            raise Exception("RENDER_FAILED")
        # Content is present. Brief settle + a single scroll so lazy blocks
        # (params, map, footer id) finish painting, then a bounded param wait.
        await human_scroll(page, fast=True)
        await page.wait_for_timeout(random.randint(*AD_WAIT_MS))
        await wait_for_param(page)  # now bounded to PARAM_WAIT_MS (~3.5s), not 8s
    finally:
        page.remove_listener("response", handle_response)
        if diag:
            print(f"  [DIAG] ready in {time.monotonic() - t0:.1f}s")

    page_title = await page.title()

    listing_id = listing_id_from_url(url)
    final_url = page.url
    if "/d/obyavlenie/" not in final_url:
        raise NotAListing(f"redirected to {final_url}")

    jd = await extract_from_jsonld(page)
    if jd.get("_sold"):
        raise NotAListing("offer marked sold/discontinued")

    params = await get_params(page)
    olx_id = await get_olx_id(page)

    if diag:
        print(f"  [DIAG] final_url   : {final_url}")
        print(f"  [DIAG] page_title  : {page_title[:120]}")
        print(f"  [DIAG] listing_id  : {listing_id}   olx_id: {olx_id}")
        print(f"  [DIAG] json-ld keys: {sorted(jd.keys())}")
        print(f"  [DIAG] params      : {params}")

    title       = jd.get("title")
    price       = jd.get("price")
    currency    = jd.get("currency")
    description = jd.get("description")
    location    = jd.get("location")
    posted_date = jd.get("posted_date")

    area         = params.get("Общая площадь") or jd.get("area")
    num_rooms    = extract_number(params.get("Количество комнат"))
    stair        = params.get("Этаж")
    total_floors = params.get("Этажность дома") or params.get("Этажей в доме")
    market_type  = params.get("Тип жилья") or params.get("Тип дома")
    negotiation  = False
    seller       = None
    seller_joined = None
    views        = jd.get("views") or views_holder["value"]

    if not title:
        for sel in ["h1", '[data-cy="ad_title"]', '[data-testid="ad-title"]']:
            try:
                el = page.locator(sel)
                if await el.count() > 0:
                    title = clean(await el.first.inner_text())
                    if title:
                        break
            except Exception:
                pass
        if not title:
            title = clean(page_title.split(" - ")[0].split(" | ")[0])

    if not price or not currency:
        try:
            price_loc = page.locator('[data-testid="ad-price-container"]')
            if await price_loc.count() > 0:
                price_text = clean(await price_loc.first.inner_text()) or ""
                if not price:
                    price = extract_number(price_text)
                low = price_text.lower()
                if not currency:
                    if "$" in price_text or "у.е" in low or "usd" in low:
                        currency = "USD"
                    elif "сум" in low or "uzs" in low or "sum" in low:
                        currency = "UZS"
                if "договорная" in low:
                    negotiation = True
        except Exception:
            pass

    if not seller:
        try:
            sl = page.locator('[data-testid="user-profile-user-name"]')
            if await sl.count() > 0:
                seller = clean(await sl.first.inner_text())
        except Exception:
            pass

    if not seller_joined:
        try:
            ms = page.locator('[data-testid="member-since"]')
            if await ms.count() > 0:
                seller_joined = clean(await ms.first.inner_text())
        except Exception:
            pass

    if not location:
        loc_raw, loc_src = await get_location(page)
        loc_clean = clean_location(loc_raw)
        if diag:
            print(f"  [DIAG] location    : src={loc_src} raw={loc_raw!r} cleaned={loc_clean!r}")
        if loc_clean:
            location = loc_clean
    if not location:
        location = location_from_title(page_title)

    if not description:
        try:
            dl = page.locator('[data-testid="ad_description"], [data-cy="ad_description"]')
            if await dl.count() > 0:
                description = clean(await dl.first.inner_text())
        except Exception:
            pass

    if not posted_date:
        try:
            date_loc = page.locator('[data-cy="ad-posted-at"], [data-testid="ad-posted-at"]')
            if await date_loc.count() > 0:
                posted_date = clean(await date_loc.first.inner_text())
        except Exception:
            pass

    if not views:
        for sel in ['[data-testid="page-view-counter"]', '[data-cy="view-count"]']:
            try:
                el = page.locator(sel)
                if await el.count() > 0:
                    views = extract_number(await el.first.inner_text())
                    if views:
                        break
            except Exception:
                pass

    if description:
        desc_low = description.lower()
        if not area:
            mm = re.search(r"(\d+[.,]?\d*)\s*м[²2]", description, re.I)
            if mm:
                area = f"{mm.group(1)} м²"
        if not num_rooms:
            mm = re.search(r"(\d+)\s*[\s-]*комнат", description, re.I)
            if mm:
                num_rooms = int(mm.group(1))
        if not market_type:
            if any(x in desc_low for x in ["вторичн"]):
                market_type = "Вторичный рынок"
            elif any(x in desc_low for x in ["новостройка", "первичн"]):
                market_type = "Новостройка"
        if not negotiation and ("договорная" in desc_low or "negotiable" in desc_low):
            negotiation = True

    area = normalize_area(area)

    return {
        "listing_id":    listing_id,
        "snapshot_date": datetime.now().strftime("%Y-%m-%d"),
        "olx_id":        olx_id,
        "title":         title,
        "price":         price,
        "currency":      currency,
        "area":          area,
        "num_rooms":     num_rooms,
        "market_type":   market_type,
        "views":         views,
        "stair":         stair,
        "total_floors":  total_floors,
        "posted_date":   posted_date,
        "scraped_date":  now_str(),
        "negotiation":   negotiation,
        "seller":        seller,
        "location":      location,
        "seller_joined": seller_joined,
        "description":   description,
        "url":           url,
    }


# ─────────────────────────────────────────────────────────────────
# BROWSER WARMUP
# ─────────────────────────────────────────────────────────────────

async def warmup_browser(page):
    try:
        print("  [browser] warming up session on OLX homepage...")
        await page.goto("https://www.olx.uz/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(random.randint(3000, 5000))
        await human_scroll(page)
        print("  [browser] session ready.")
    except Exception as e:
        print(f"  [browser] warmup failed (continuing anyway): {e}")


# ─────────────────────────────────────────────────────────────────
# RUN ORCHESTRATION
# ─────────────────────────────────────────────────────────────────

_scrape_lock = asyncio.Lock()


async def run_scrape():
    if _scrape_lock.locked():
        print(f"⏭  scrape already in progress (in-process) — skipping this trigger ({now_str()})")
        return 0
    async with _scrape_lock:
        return await _run_scrape_inner()


async def _run_scrape_inner():
    print(f"\n{'='*55}")
    print(f"  SCRAPE STARTED  —  {now_str()}")
    print(f"{'='*55}\n")

    try:
        engine = get_engine()
    except Exception as e:
        print(f"FATAL DB ERROR: {e}")
        return 0

    lock_conn = try_acquire_run_lock(engine)
    if lock_conn is None:
        print(f"⏭  another run holds the DB advisory lock — skipping ({now_str()})")
        return 0

    try:
        try:
            ensure_table(engine)
            backfill_locations(engine)
            print("✓ Database connected.")
        except Exception as e:
            print(f"FATAL DB ERROR: {e}")
            return 0

        batch_data = []
        ad_counter = 0
        skipped_removed = 0
        skipped_recent = 0
        budget_hit = False
        start_ts = time.monotonic()
        budget_secs = MAX_RUNTIME_HOURS * 3600 if MAX_RUNTIME_HOURS > 0 else None

        async with async_playwright() as p:
            print("Collecting listing links...")
            all_links = list(set(await get_all_links(p, BASE_URL, MAX_PAGES)))
            print(f"\nTotal unique links: {len(all_links)}")

            done_today = load_done_today(engine)
            if done_today:
                before = len(all_links)
                all_links = [l for l in all_links
                             if listing_id_from_url(l) not in done_today]
                skipped_recent = before - len(all_links)
                print(f"  Resume: {skipped_recent} listings already snapshotted today "
                      f"skipped — {len(all_links)} to scrape this run.")

            browser, page = await make_browser_page(p)
            await warmup_browser(page)

            for idx, link in enumerate(all_links, start=1):
                ad_counter += 1

                if budget_secs and (time.monotonic() - start_ts) > budget_secs:
                    budget_hit = True
                    print(f"\n  ⏲  Reached {MAX_RUNTIME_HOURS:g}h runtime budget at "
                          f"listing {idx}/{len(all_links)} — stopping cleanly; "
                          f"next run resumes the rest.\n")
                    break

                if idx > 1 and (idx - 1) % BROWSER_RESTART_EVERY == 0:
                    print(f"\n  ── restarting browser at listing {idx} ──\n")
                    browser, page = await restart_browser(p, browser)
                    await warmup_browser(page)

                print(f"[{idx}/{len(all_links)}] {link.split('/')[-1][:60]}")
                diag = ad_counter <= DIAG_DUMP
                data = None
                skip_permanently = False

                for attempt in range(AD_ATTEMPTS):
                    try:
                        data = await scrape_ad(page, link, diag=diag)
                        has_detail = data and (
                            data.get("price") or data.get("area") or data.get("num_rooms")
                        )
                        if data and not (data.get("title") and has_detail):
                            raise Exception("RENDER_FAILED")
                        break
                    except NotAListing as e:
                        print(f"  ⏭  skipped (not a listing): {e}")
                        skip_permanently = True
                        data = None
                        break
                    except Exception as e:
                        err = str(e)
                        if "BLOCKED_403" in err:
                            # Genuine 403/access-denied (not a CF interstitial —
                            # those are now waited out inside scrape_ad). IP may be
                            # flagged, so cool down progressively.
                            wait = 15 * (attempt + 1)
                            print(f"  BLOCKED — retry {attempt+1}/{AD_ATTEMPTS} in {wait}s")
                            await asyncio.sleep(wait)
                        elif "RENDER_FAILED" in err:
                            # Content was reachable but not ready in time; a fresh
                            # navigation usually fixes it. Retry fast, escalate gently.
                            wait = 2 + attempt * 3
                            print(f"  Page rendered incomplete — retry {attempt+1}/{AD_ATTEMPTS} in {wait}s")
                            await asyncio.sleep(wait)
                            data = None
                        elif any(x in err for x in [
                            "Target page", "browser has been closed",
                            "page has been closed", "Browser closed",
                            "context or browser", "Target closed",
                            "Timeout", "timeout", "Page crashed", "crashed",
                        ]):
                            print(f"  Browser crashed/timeout — restarting (attempt {attempt+1}/{AD_ATTEMPTS})")
                            browser, page = await restart_browser(p, browser)
                            await warmup_browser(page)
                        else:
                            print(f"  FAILED: {e}")
                            break

                if skip_permanently:
                    skipped_removed += 1
                elif data:
                    batch_data.append(data)
                    print(
                        f"  ✓  rooms={data['num_rooms']}  area={data['area']}  "
                        f"floor={data['stair']}/{data['total_floors']}  "
                        f"price={data['price']} {data['currency']}  "
                        f"type={str(data['market_type'])[:18]}  loc={str(data['location'])[:30]}"
                    )
                else:
                    print("  ✗ skipped (incomplete after retries)")

                if len(batch_data) >= BATCH_SIZE:
                    save_batch_to_db(batch_data, engine)
                    batch_data.clear()

                await flush_page(page)
                await short_delay(*BETWEEN_ADS)

                if ad_counter % LONG_BREAK_EVERY == 0:
                    secs = random.uniform(*LONG_BREAK_SECS)
                    print(f"\n  ── pause {secs:.0f}s ──\n")
                    await asyncio.sleep(secs)

            try:
                await browser.close()
            except Exception:
                pass
            kill_orphan_chrome()

        if batch_data:
            save_batch_to_db(batch_data, engine)

        status = "STOPPED (budget)" if budget_hit else "DONE"
        elapsed_h = (time.monotonic() - start_ts) / 3600
        print(f"\n{'='*55}")
        print(f"  {status}  —  {ad_counter} ads processed, {skipped_removed} removed/skipped, "
              f"{skipped_recent} resumed-skip  —  {elapsed_h:.1f}h  —  {now_str()}")
        print(f"{'='*55}\n")
        return ad_counter
    finally:
        release_run_lock(lock_conn)


# ─────────────────────────────────────────────────────────────────
# ENTRYPOINT  — SINGLE scheduler. No double-trigger.
#
# Reads RUN_ON_START and SCRAPE_HOUR from env (your existing Railway vars).
# The boot run (if enabled) is awaited in the SAME event loop as the cron, so
# the in-process _scrape_lock genuinely protects against overlap. max_instances=1
# is a second backstop; the Postgres advisory lock is a third (replicas/handoffs).
#
# TIMEZONE: SCRAPE_HOUR is interpreted in TZ_NAME (default Asia/Tashkent). Set
# TZ_NAME=UTC if you actually want UTC.
# ─────────────────────────────────────────────────────────────────
from apscheduler.schedulers.asyncio import AsyncIOScheduler

RUN_ON_START = os.getenv("RUN_ON_START", "false").lower() == "true"
SCRAPE_HOUR  = int(os.getenv("SCRAPE_HOUR", "6"))
SCRAPE_MIN   = int(os.getenv("SCRAPE_MIN", "0"))
TZ_NAME      = os.getenv("TZ_NAME", "Asia/Tashkent")


async def _main():
    scheduler = AsyncIOScheduler(timezone=TZ_NAME)
    scheduler.add_job(
        run_scrape, "cron",
        hour=SCRAPE_HOUR, minute=SCRAPE_MIN,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    print(f"Scheduler started — daily at {SCRAPE_HOUR:02d}:{SCRAPE_MIN:02d} {TZ_NAME} "
          f"(run_on_start={RUN_ON_START}).")

    if RUN_ON_START:
        await run_scrape()

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(_main())
