"""
OLX scraper — adapted from olx.ipynb.
Uses Playwright (async) to scrape apartment listings from olx.uz.
"""

import asyncio
import random
import re
from datetime import datetime

from playwright.async_api import async_playwright


# ─────────────────────────────────────────────────────────────────
# TIMING PROFILES  (human-like, not too slow)
# ─────────────────────────────────────────────────────────────────
AD_WAIT_MS       = (1800, 3500)
BETWEEN_ADS      = (2.0, 4.0)
BETWEEN_LIST     = (4.0, 8.0)
LONG_BREAK_EVERY = 15
LONG_BREAK_SECS  = (10, 15)
SCROLL_PASSES    = (2, 3)
SCROLL_DIST_PX   = (500, 1200)
SCROLL_PAUSE     = (0.4, 0.9)


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

def clean(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r"\s+", " ", text).strip() or None


def extract_number(text: str | None) -> int | None:
    if not text:
        return None
    text = text.replace("\u00a0", "").replace(" ", "")
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def short_delay(a: float, b: float) -> None:
    await asyncio.sleep(random.uniform(a, b))


async def human_scroll(page, fast: bool = False) -> None:
    passes = 1 if fast else random.randint(*SCROLL_PASSES)
    for _ in range(passes):
        dist = random.randint(*SCROLL_DIST_PX)
        await page.mouse.wheel(0, dist)
        await asyncio.sleep(random.uniform(*SCROLL_PAUSE))
        if not fast and random.random() < 0.3:
            await asyncio.sleep(random.uniform(0.5, 1.2))


# ─────────────────────────────────────────────────────────────────
# LINK COLLECTION
# ─────────────────────────────────────────────────────────────────

def page_url(base: str, page_num: int) -> str:
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}page={page_num}" if page_num > 1 else base


async def get_all_links(page, base_url: str, max_pages: int) -> list[str]:
    all_links: set[str] = set()

    for pg in range(1, max_pages + 1):
        url = page_url(base_url, pg)
        print(f"── LIST PAGE {pg}/{max_pages}: {url}")

        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(random.randint(2000, 4000))
        await human_scroll(page)

        hrefs = await page.locator("a").evaluate_all(
            "elements => elements.map(e => e.href)"
        )
        before = len(all_links)
        for href in hrefs:
            if href and "/d/obyavlenie/" in href:
                all_links.add(href.split("?")[0])

        print(f"   +{len(all_links) - before} new  (total {len(all_links)})")

        if pg < max_pages:
            await short_delay(*BETWEEN_LIST)

    return list(all_links)


# ─────────────────────────────────────────────────────────────────
# STRUCTURED ATTRIBUTES
# ─────────────────────────────────────────────────────────────────

async def get_list_container_attrs(page) -> dict:
    attrs: dict = {}
    try:
        container = page.locator('[data-nx-name="ListContainer"]')
        if await container.count() == 0:
            return attrs
        rows = container.locator("li, p")
        count = await rows.count()
        for i in range(count):
            row_text = clean(await rows.nth(i).inner_text())
            if not row_text:
                continue
            if ":" in row_text:
                key, _, val = row_text.partition(":")
                attrs[clean(key)] = clean(val)
            else:
                parts = row_text.split(None, 1)
                if len(parts) == 2:
                    attrs[clean(parts[0])] = clean(parts[1])
    except Exception as e:
        print(f"  [attrs] {e}")
    return attrs


# ─────────────────────────────────────────────────────────────────
# SCRAPE ONE AD
# ─────────────────────────────────────────────────────────────────

async def scrape_ad(page, url: str) -> dict | None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(random.randint(*AD_WAIT_MS))
        await human_scroll(page)

        page_title = await page.title()
        if any(x in page_title.lower() for x in ["403", "access denied", "captcha", "just a moment"]):
            print(f"  BLOCKED: {url}")
            return None

        # 1. Listing ID
        listing_id = None
        try:
            label2 = page.locator('[data-nx-name="Label2"]')
            if await label2.count() > 0:
                raw = clean(await label2.first.inner_text()) or ""
                m = re.search(r"(\d{5,})", raw)
                if m:
                    listing_id = m.group(1)
        except Exception:
            pass

        if not listing_id:
            try:
                bt = clean(await page.text_content("body")) or ""
                m = re.search(r"\bID[:\s#]*(\d{5,})", bt, re.I)
                if m:
                    listing_id = m.group(1)
            except Exception:
                pass

        if not listing_id:
            m = re.search(r"-(ID[A-Za-z0-9]+)\.html", url)
            if m:
                listing_id = m.group(1)

        # 2. Title
        title = None
        try:
            h1 = page.locator("h1")
            if await h1.count() > 0:
                title = clean(await h1.first.inner_text())
        except Exception:
            pass
        if not title:
            title = clean(page_title.split(":")[0])

        # 3. Price & currency
        price = None
        currency = None
        try:
            price_loc = page.locator('[data-testid="ad-price-container"]')
            price_text = ""
            if await price_loc.count() > 0:
                price_text = clean(await price_loc.first.inner_text()) or ""
            else:
                price_text = clean(await page.text_content("body")) or ""
            price = extract_number(price_text)
            low = price_text.lower()
            if "$" in price_text or "у.е" in low or "usd" in low:
                currency = "USD"
            elif "сум" in low or "uzs" in low or "sum" in low:
                currency = "UZS"
        except Exception:
            pass

        # 4. Structured attrs
        attrs = await get_list_container_attrs(page)

        def pick(keys):
            for k in keys:
                if attrs.get(k):
                    return attrs[k]
            return None

        area_raw   = pick(["Общая площадь", "Umumiy maydoni", "Умумий майдони"])
        rooms_raw  = pick(["Количество комнат", "Xonalar soni", "Xona soni"])
        market_raw = pick(["Тип жилья", "Uy-joy turi"])
        stair_raw  = pick(["Этаж", "Qavat"])

        area        = clean(area_raw)
        num_rooms   = extract_number(rooms_raw)
        market_type = clean(market_raw)
        stair       = clean(stair_raw)

        if not all([area, num_rooms, stair]):
            try:
                bt = clean(await page.text_content("body")) or ""
                if not area:
                    m = re.search(r"Общая площадь[:\s]*([^\n\r]{1,30}?)(?=\s*(?:Этаж|Количество|Тип|$))", bt, re.I)
                    if m:
                        val = clean(m.group(1))
                        if val and re.search(r"\d", val) and len(val) < 30:
                            area = val
                if not num_rooms:
                    m = re.search(r"Количество комнат[:\s]*(\d+)", bt, re.I)
                    if m:
                        num_rooms = int(m.group(1))
                if not market_type:
                    m = re.search(r"Тип жилья[:\s]*([^\n\r]{1,60}?)(?=\s*(?:Этаж|Количество|Общая|$))", bt, re.I)
                    if m:
                        val = clean(m.group(1))
                        if val and len(val) < 60:
                            market_type = val
                if not stair:
                    m = re.search(r"\bЭтаж[:\s]*([^\n\r]{1,30}?)(?=\s*(?:Количество|Общая|Тип|$))", bt, re.I)
                    if m:
                        val = clean(m.group(1))
                        if val and len(val) < 30:
                            stair = val
            except Exception:
                pass

        # 5. Views
        views = None
        try:
            views_loc = page.locator('[data-testid="page-view-counter"]')
            if await views_loc.count() > 0:
                views = extract_number(await views_loc.first.inner_text())
        except Exception:
            pass

        # 6. Posted date
        posted_date = None
        try:
            date_loc = page.locator('[data-cy="ad-posted-at"], [data-testid="ad-posted-at"]')
            if await date_loc.count() > 0:
                posted_date = clean(await date_loc.first.inner_text())
            else:
                bt = clean(await page.text_content("body")) or ""
                m = re.search(r"Опубликовано[:\s]*([^\n\r]{3,40})", bt, re.I)
                if not m:
                    m = re.search(r"Дата публикации[:\s]*([^\n\r]{3,40})", bt, re.I)
                if m:
                    posted_date = clean(m.group(1))
        except Exception:
            pass

        # 7. Negotiation
        negotiation = False
        try:
            p4 = page.locator('[data-nx-name="P4"]')
            if await p4.count() > 0:
                p4_text = (clean(await p4.first.inner_text()) or "").lower()
                if any(x in p4_text for x in ["договорная", "negotiable", "kelishiladi"]):
                    negotiation = True
            if not negotiation:
                bt = (clean(await page.text_content("body")) or "").lower()
                if "договорная" in bt or "negotiable" in bt:
                    negotiation = True
        except Exception:
            pass

        # 8. Seller
        seller = None
        try:
            seller_loc = page.locator('[data-testid="user-profile-user-name"]')
            if await seller_loc.count() > 0:
                seller = clean(await seller_loc.first.inner_text())
        except Exception:
            pass

        # 9. Location
        location = None
        try:
            texts = await page.locator("p, span").all_inner_texts()
            for text in texts:
                text = clean(text)
                if not text:
                    continue
                low = text.lower()
                if any(x in low for x in ["район", "ташкент", "toshkent", "область"]):
                    if len(text) < 120:
                        location = text
                        break
        except Exception:
            pass

        # 10. Seller joined
        seller_joined = None
        try:
            ms_loc = page.locator('[data-testid="member-since"]')
            if await ms_loc.count() > 0:
                seller_joined = clean(await ms_loc.first.inner_text())
        except Exception:
            pass
        if not seller_joined:
            try:
                texts = await page.locator("span, p").all_inner_texts()
                for text in texts:
                    text = clean(text)
                    if text and "на olx с" in text.lower():
                        seller_joined = text
                        break
            except Exception:
                pass

        # 11. Description
        description = None
        try:
            desc_loc = page.locator('[data-testid="ad_description"]')
            if await desc_loc.count() > 0:
                description = clean(await desc_loc.first.inner_text())
        except Exception:
            pass

        # 12. Description fallback
        if description:
            desc_low = description.lower()
            if not area:
                m = re.search(r"(\d+[.,]?\d*)\s*м[²2]", description, re.I)
                if m:
                    area = m.group(1) + " м²"
            if not num_rooms:
                m = re.search(r"(\d+)[\s-]*комнат", description, re.I)
                if not m:
                    m = re.search(r"комнат[:\s]*(\d+)", description, re.I)
                if m:
                    num_rooms = int(m.group(1))
            if not stair:
                m = re.search(r"этаж[:\s]*([\d\s/\-]+(?:из\s*\d+)?)", description, re.I)
                if m:
                    stair = clean(m.group(1))
            if not market_type:
                if any(x in desc_low for x in ["вторичный", "вторичка"]):
                    market_type = "Вторичный рынок"
                elif any(x in desc_low for x in ["новостройка", "первичный"]):
                    market_type = "Новостройка"

        return {
            "listing_id":    listing_id,
            "title":         title,
            "price":         price,
            "currency":      currency,
            "area":          area,
            "num_rooms":     num_rooms,
            "market_type":   market_type,
            "views":         views,
            "stair":         stair,
            "posted_date":   posted_date,
            "negotiation":   negotiation,
            "seller":        seller,
            "location":      location,
            "seller_joined": seller_joined,
            "description":   description,
            "url":           url,
        }

    except Exception as e:
        print(f"  FAILED: {e}")
        return None


# ─────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────

async def run_scraper(base_url: str, max_pages: int) -> list[dict]:
    """
    Run the full scrape and return a list of listing dicts.
    Called by the scheduler and the /scraper/run endpoint.
    """
    all_data: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            slow_mo=80,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--single-process",
                "--no-zygote",
                "--disable-accelerated-2d-canvas",
                "--disable-web-security",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="ru-RU",
            timezone_id="Asia/Tashkent",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU','ru','en-US'] });
        """)

        # Warm-up
        print("WARMING SESSION...")
        await page.goto("https://www.olx.uz/", wait_until="domcontentloaded")
        await short_delay(3, 5)
        await human_scroll(page, fast=True)

        # Collect links
        all_links = await get_all_links(page, base_url, max_pages)
        all_links = list(set(all_links))
        print(f"\nTOTAL UNIQUE LINKS: {len(all_links)}")

        # Scrape each ad
        for idx, link in enumerate(all_links, start=1):
            print(f"[{idx}/{len(all_links)}] {link.split('/')[-1]}")
            data = await scrape_ad(page, link)
            if data:
                all_data.append(data)
                print(f"  ✓ {(data['title'] or '')[:50]}")
            else:
                print("  ✗ skipped")

            await short_delay(*BETWEEN_ADS)

            if idx % LONG_BREAK_EVERY == 0:
                secs = random.uniform(*LONG_BREAK_SECS)
                print(f"\n  ── pause {secs:.0f}s ──\n")
                await asyncio.sleep(secs)

        await browser.close()

    return all_data
