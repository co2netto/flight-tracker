"""
BKK <-> ZQN Flight Price Tracker
- Alerts on every check regardless of price
- Logs price history to prices.csv in the repo
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

# ── CSV logger ─────────────────────────────────────────────────────────────────

def load_last_price(origin: str, destination: str) -> int | None:
    """Read the last recorded cheapest price for a route from CSV."""
    if not os.path.exists(CSV_FILE):
        return None
    with open(CSV_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    route_rows = [r for r in rows
                  if r["origin"] == origin and r["destination"] == destination]
    if not route_rows:
        return None
    try:
        return int(route_rows[-1]["cheapest_price_thb"])
    except (ValueError, KeyError):
        return None

def append_to_csv(origin: str, destination: str, date: str,
                  cheapest: dict, all_flights: list):
    """Append one row per check to prices.csv."""
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
    url = (
        f"https://www.google.com/travel/flights/search"
        f"?q=Flights+from+{origin}+to+{destination}"
        f"&curr=THB&hl=en"
    )
    results = []

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
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_selector('[data-ved]', timeout=30000)
            await asyncio.sleep(3)

            flights = await page.evaluate("""
                () => {
                    const results = [];

                    // Strategy 1: look for list items that contain a THB price
                    const candidates = Array.from(document.querySelectorAll('li, [role="listitem"]'));
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

                    // Strategy 2: fallback — scan all text if strategy 1 found nothing
                    if (results.length === 0) {
                        const allText = document.body.innerText;
                        const matches = [...allText.matchAll(/฿\\s?([\\d,]+)/g)];
                        matches.forEach(m => {
                            const price = parseInt(m[1].replace(/,/g, ''));
                            if (price >= 1000 && price <= 500000) {
                                results.push({ airline: 'Unknown', price, duration: '', stops: '' });
                            }
                        });
                    }

                    // Deduplicate by airline + price
                    const seen = new Set();
                    return results.filter(r => {
                        const key = r.airline + r.price;
                        if (seen.has(key)) return false;
                        seen.add(key);
                        return true;
                    });
                }
            """)
            results = flights if flights else []

        except PlaywrightTimeout:
            print(f"Timeout scraping {origin} → {destination}")
        except Exception as e:
            print(f"Error scraping {origin} → {destination}: {e}")
        finally:
            await browser.close()

    return results

# ── Price change indicator ─────────────────────────────────────────────────────

def price_change_label(current: int, previous: int | None) -> str:
    if previous is None:
        return "🆕 First check"
    diff = current - previous
    if diff == 0:
        return "➡️ No change from last check"
    elif diff < 0:
        return f"🟢 Down ฿{abs(diff):,} from last check"
    else:
        return f"🔴 Up ฿{diff:,} from last check"

# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    for route in ROUTES:
        origin      = route["origin"]
        destination = route["destination"]
        date        = route["date"]

        print(f"Checking {origin} → {destination} on {date}...")
        flights = await scrape_google_flights(origin, destination, date)

        if not flights:
            print(f"  No results found (site may have changed layout)")
            send_telegram(
                f"⚠️ <b>{origin} → {destination}</b>\n"
                f"No results found at {now}.\n"
                f"Google Flights may have updated its layout."
            )
            continue

        flights.sort(key=lambda x: x["price"])
        cheapest   = flights[0]
        last_price = load_last_price(origin, destination)
        change     = price_change_label(cheapest["price"], last_price)

        # Save to CSV
        append_to_csv(origin, destination, date, cheapest, flights)
        print(f"  Cheapest: ฿{cheapest['price']:,} — {cheapest['airline']} | {change}")

        # Top 5 results
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
            + f"\n\n<a href='https://www.google.com/travel/flights/search"
              f"?q=Flights+from+{origin}+to+{destination}&curr=THB&hl=en'>"
              f"View on Google Flights →</a>"
        )
        send_telegram(msg)

if __name__ == "__main__":
    asyncio.run(main())
