"""
BKK <-> ZQN Flight Price Tracker
- Fills the Google Flights search form like a real user
- Alerts on every check regardless of price
- Logs price history to prices.csv in the repo
- Sends debug screenshot to Telegram on failure
"""

import os
import csv
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Configuration ──────────────────────────────────────────────────────────────

ROUTES = [
    {"origin": "BKK", "destination": "ZQN", "date": "2025-07-17"},
    {"origin": "BKK", "destination": "ZQN", "date": "2025-07-18"},
    {"origin": "BKK", "destination": "ZQN", "date": "2025-07-19"},
    {"origin": "BKK", "destination": "ZQN", "date": "2025-07-20"},
    {"origin": "ZQN", "destination": "BKK", "date": "2025-07-31"},
    {"origin": "ZQN", "destination": "BKK", "date": "2025-08-01"},
    {"origin": "ZQN", "destination": "BKK", "date": "2025-08-02"},
]

ADULTS      = 1
CABIN_CLASS = "economy"  # economy | premium_economy | business | first

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

CSV_FILE = "prices.csv"

# ── Telegram ───────────────────────────────────────────────────────────────────

import urllib.request, json

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)

def send_telegram_photo(photo_path: str, caption: str = ""):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as f:
        photo_data = f.read()
    boundary = "FormBoundaryABC123"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{TELEGRAM_CHAT_ID}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="caption"\r\n\r\n'
        f"{caption}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="debug.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + photo_data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        print(f"  Telegram photo response: {resp.status}")
    except Exception as e:
        print(f"  Could not send screenshot to Telegram: {e}")

# ── CSV logger ─────────────────────────────────────────────────────────────────

def load_last_price(origin: str, destination: str, date: str) -> int | None:
    if not os.path.exists(CSV_FILE):
        return None
    with open(CSV_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    route_rows = [r for r in rows
                  if r["origin"] == origin
                  and r["destination"] == destination
                  and r["travel_date"] == date]
    if not route_rows:
        return None
    try:
        return int(route_rows[-1]["cheapest_price_thb"])
    except (ValueError, KeyError):
        return None

def append_to_csv(origin, destination, date, cheapest, all_flights):
    file_exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "checked_at", "origin", "destination", "travel_date",
            "cheapest_price_thb", "cheapest_airline",
            "cheapest_duration", "cheapest_stops", "num_results"
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "checked_at":         datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "origin":             origin,
            "destination":        destination,
            "travel_date":        date,
            "cheapest_price_thb": cheapest["price"],
            "cheapest_airline":   cheapest["airline"],
            "cheapest_duration":  cheapest.get("duration", ""),
            "cheapest_stops":     cheapest.get("stops", ""),
            "num_results":        len(all_flights),
        })

# ── Scraper ────────────────────────────────────────────────────────────────────

async def scrape_google_flights(origin: str, destination: str, date: str) -> list[dict]:
    results      = []
    debug_prefix = f"debug_{origin}_{destination}_{date}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="Asia/Bangkok",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        try:
            # ── Step 1: Load homepage ──────────────────────────────────────
            print(f"  Loading Google Flights...")
            await page.goto(
                "https://www.google.com/travel/flights?hl=en&curr=THB",
                wait_until="networkidle", timeout=60000
            )
            await asyncio.sleep(2)

            # Dismiss any cookie / consent dialog if present
            for selector in ['button:has-text("Accept all")', 'button:has-text("Reject all")',
                              'button:has-text("Accept")', '[aria-label="Accept all"]']:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        await asyncio.sleep(1)
                        break
                except Exception:
                    pass

            # ── Step 2: Fill "Where from?" ────────────────────────────────
            print(f"  Entering origin: {origin}")
            from_field = page.locator('input[placeholder="Where from?"]').first
            await from_field.click()
            await from_field.click(click_count=3)
            await from_field.type(origin, delay=100)
            await asyncio.sleep(1.5)
            await page.keyboard.press("ArrowDown")
            await page.keyboard.press("Enter")
            await asyncio.sleep(1)

            # ── Step 3: Fill "Where to?" ──────────────────────────────────
            print(f"  Entering destination: {destination}")
            to_field = page.locator('input[placeholder="Where to?"]').first
            await to_field.click()
            await to_field.click(click_count=3)
            await to_field.type(destination, delay=100)
            await asyncio.sleep(1.5)
            await page.keyboard.press("ArrowDown")
            await page.keyboard.press("Enter")
            await asyncio.sleep(1)

            # ── Step 4: Fill departure date ───────────────────────────────
            print(f"  Entering date: {date}")
            depart_field = page.locator('input[placeholder="Departure"]').first
            await depart_field.click()
            await asyncio.sleep(1)
            await depart_field.click(click_count=3)
            await depart_field.type(date, delay=80)
            await page.keyboard.press("Enter")
            await asyncio.sleep(1)

            # ── Step 5: Search ────────────────────────────────────────────
            print(f"  Clicking Search...")
            search_btn = page.locator('button:has-text("Search")').first
            await search_btn.click()
            await asyncio.sleep(7)  # Wait for results to load

            # ── Step 6: Screenshot ────────────────────────────────────────
            screenshot_path = f"{debug_prefix}.png"
            await page.screenshot(path=screenshot_path, full_page=False)
            print(f"  Screenshot saved: {screenshot_path}")

            title     = await page.title()
            page_text = await page.inner_text("body")
            print(f"  Page title: {title}")
            print(f"  Page text preview: {page_text[:500]}")

            # ── Step 7: Extract prices ─────────────────────────────────────
            flights = await page.evaluate("""
                () => {
                    const results = [];

                    // Strategy 1: list/row elements containing a THB price
                    const candidates = Array.from(
                        document.querySelectorAll('li, [role="listitem"], [role="row"]')
                    );
                    candidates.forEach(card => {
                        const text = card.innerText || '';
                        const priceMatch = text.match(/฿\\s?([\\d,]+)/);
                        if (!priceMatch) return;
                        const price = parseInt(priceMatch[1].replace(/,/g, ''));
                        if (price < 1000 || price > 500000) return;

                        const durMatch = text.match(/(\\d{1,2}\\s?hr?\\s?\\d{0,2}\\s?min?|\\d{1,2}h\\s?\\d{0,2}m)/i);
                        const duration = durMatch ? durMatch[0].trim() : '';

                        const stopMatch = text.match(/(nonstop|\\d+\\s+stop)/i);
                        const stops = stopMatch ? stopMatch[0].trim() : '';

                        const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
                        const airline = lines[0] || 'Unknown';

                        results.push({ airline, price, duration, stops });
                    });

                    // Strategy 2: full page scan fallback
                    if (results.length === 0) {
                        const matches = [...document.body.innerText.matchAll(/฿\\s?([\\d,]+)/g)];
                        matches.forEach(m => {
                            const price = parseInt(m[1].replace(/,/g, ''));
                            if (price >= 1000 && price <= 500000)
                                results.push({ airline: 'Unknown', price, duration: '', stops: '' });
                        });
                    }

                    // Deduplicate
                    const seen = new Set();
                    return results.filter(r => {
                        const key = r.airline + r.price;
                        if (seen.has(key)) return false;
                        seen.add(key); return true;
                    });
                }
            """)

            results = flights if flights else []
            print(f"  Found {len(results)} result(s)")

            if not results:
                print(f"  Sending screenshot to Telegram: {screenshot_path}")
                import os as _os
                print(f"  Screenshot file exists: {_os.path.exists(screenshot_path)}, size: {_os.path.getsize(screenshot_path) if _os.path.exists(screenshot_path) else 0} bytes")
                send_telegram_photo(
                    screenshot_path,
                    f"⚠️ {origin}→{destination} {date}: 0 prices found.\nTitle: {title}"
                )

        except PlaywrightTimeout:
            print(f"  Timeout")
        except Exception as e:
            print(f"  Error: {e}")
            import traceback; traceback.print_exc()
        finally:
            await browser.close()

    return results

# ── Price change indicator ─────────────────────────────────────────────────────

def price_change_label(current: int, previous: int | None) -> str:
    if previous is None:
        return "🆕 First check"
    diff = current - previous
    if diff == 0:   return "➡️ No change from last check"
    if diff < 0:    return f"🟢 Down ฿{abs(diff):,} from last check"
    return              f"🔴 Up ฿{diff:,} from last check"

# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # DEBUG: only first route until confirmed working — remove [:1] after that
    for route in ROUTES[:1]:
        origin      = route["origin"]
        destination = route["destination"]
        date        = route["date"]

        print(f"\nChecking {origin} → {destination} on {date}...")
        flights = await scrape_google_flights(origin, destination, date)

        if not flights:
            print(f"  No results — screenshot sent to Telegram")
            continue

        flights.sort(key=lambda x: x["price"])
        cheapest   = flights[0]
        last_price = load_last_price(origin, destination, date)
        change     = price_change_label(cheapest["price"], last_price)

        append_to_csv(origin, destination, date, cheapest, flights)
        print(f"  Cheapest: ฿{cheapest['price']:,} — {cheapest['airline']} | {change}")

        lines = []
        for i, f in enumerate(flights[:5], 1):
            stop_info = f"· {f['stops']}" if f.get("stops") else ""
            lines.append(
                f"  {i}. {f['airline']} — <b>฿{f['price']:,}</b>"
                f"  {f.get('duration', '')} {stop_info}".strip()
            )

        msg = (
            f"✈️ <b>{origin} → {destination}</b>  |  {date}\n"
            f"🕐 {now}  |  {CABIN_CLASS.replace('_', ' ').title()}\n"
            f"{change}\n\n"
            + "\n".join(lines)
            + f"\n\n<a href='https://www.google.com/travel/flights?hl=en&curr=THB'>"
              f"Open Google Flights →</a>"
        )
        send_telegram(msg)

if __name__ == "__main__":
    asyncio.run(main())
