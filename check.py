#!/usr/bin/env python3
"""
On-demand flight price check.

Run:  python check.py

Fetches real-time Google Flights prices for each configured route,
prints a summary to the terminal, and sends the same summary to Telegram.
History is kept in price_history.json so you get trend arrows vs. last run.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from fast_flights import FlightData, Passengers, get_flights

# ---------- Configuration ----------

ROUTES = [
    {"origin": "BKK", "destination": "ZQN", "date": "2026-07-16"},
    {"origin": "BKK", "destination": "ZQN", "date": "2026-07-17"},
    {"origin": "BKK", "destination": "ZQN", "date": "2026-07-18"},
    {"origin": "DMK", "destination": "ZQN", "date": "2026-07-16"},
    {"origin": "DMK", "destination": "ZQN", "date": "2026-07-17"},
    {"origin": "DMK", "destination": "ZQN", "date": "2026-07-18"},
    {"origin": "ZQN", "destination": "BKK", "date": "2026-08-01"},
    {"origin": "ZQN", "destination": "BKK", "date": "2026-08-02"},
]

ADULTS = 1
SEAT = "economy"     # economy | premium-economy | business | first
CURRENCY_LABEL = "THB"  # Google Flights returns prices in your locale's currency

HISTORY_FILE = Path("price_history.json")


# ---------- Google Flights (via fast-flights) ----------

def search_cheapest(origin: str, destination: str, date: str):
    """
    Returns (price_int, airline_name, stops_int) or (None, None, None).
    """
    try:
        result = get_flights(
            flight_data=[FlightData(date=date, from_airport=origin, to_airport=destination)],
            trip="one-way",
            seat=SEAT,
            passengers=Passengers(adults=ADULTS, children=0, infants_in_seat=0, infants_on_lap=0),
            fetch_mode="fallback",
        )
    except Exception as e:
        print(f"  fetch error: {e}", file=sys.stderr)
        return None, None, None

    if not result or not getattr(result, "flights", None):
        return None, None, None

    # Each flight has .price like "$1,234" or "฿42,500"; strip non-digits.
    def parse_price(p):
        if not p:
            return None
        digits = "".join(ch for ch in str(p) if ch.isdigit())
        return int(digits) if digits else None

    priced = []
    for f in result.flights:
        val = parse_price(getattr(f, "price", None))
        if val is not None:
            priced.append((val, f))
    if not priced:
        return None, None, None

    priced.sort(key=lambda x: x[0])
    price, flight = priced[0]
    airline = getattr(flight, "name", "??") or "??"
    stops = getattr(flight, "stops", 0) or 0
    return price, airline, stops


# ---------- History ----------

def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {}


def save_history(history: dict) -> None:
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def route_key(r: dict) -> str:
    return f"{r['origin']}-{r['destination']}-{r['date']}"


# ---------- Telegram ----------

def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    resp.raise_for_status()


def trend_marker(current: float, previous):
    if previous is None:
        return "🆕"
    if current < previous:
        return f"🟢↓ -{previous - current:.0f}"
    if current > previous:
        return f"🔴↑ +{current - previous:.0f}"
    return "⚪→"


# ---------- Main ----------

def main() -> int:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        print("ERROR: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars", file=sys.stderr)
        return 1

    history = load_history()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [f"<b>✈️ Flight check — {ts}</b>", ""]
    outbound_lines, return_lines = [], []

    for route in ROUTES:
        k = route_key(route)
        print(f"Checking {k}…")
        prev = history.get(k, {}).get("last_price")
        price, airline, stops = search_cheapest(
            route["origin"], route["destination"], route["date"]
        )

        if price is None:
            line = (
                f"<code>{route['origin']}→{route['destination']} "
                f"{route['date']}</code>  no data"
            )
        else:
            marker = trend_marker(price, prev)
            stop_txt = "direct" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}"
            line = (
                f"<code>{route['origin']}→{route['destination']} "
                f"{route['date']}</code>  "
                f"<b>{price:,} {CURRENCY_LABEL}</b> "
                f"({airline[:20]}, {stop_txt}) {marker}"
            )
            entry = history.get(k, {"history": []})
            entry["last_price"] = price
            entry["last_checked"] = ts
            entry["history"] = (entry.get("history", []) + [{"ts": ts, "price": price}])[-100:]
            if "min_price" not in entry or price < entry["min_price"]:
                entry["min_price"] = price
                entry["min_price_at"] = ts
            history[k] = entry

        if route["destination"] == "ZQN":
            outbound_lines.append(line)
        else:
            return_lines.append(line)

        time.sleep(1)  # be polite to Google

    if outbound_lines:
        lines.append("<b>Outbound → ZQN</b>")
        lines.extend(outbound_lines)
        lines.append("")
    if return_lines:
        lines.append("<b>Return ← BKK</b>")
        lines.extend(return_lines)
        lines.append("")

    lows = [(k, v["min_price"]) for k, v in history.items() if "min_price" in v]
    if lows:
        lows.sort(key=lambda x: x[1])
        lines.append("<b>All-time lows (top 3)</b>")
        for k, p in lows[:3]:
            lines.append(f"<code>{k}</code>  {p:,} {CURRENCY_LABEL}")

    message = "\n".join(lines)

    # Print to terminal (strip HTML tags for readability)
    import re
    clean = re.sub(r"<[^>]+>", "", message)
    print()
    print(clean)
    print()

    try:
        send_telegram(bot_token, chat_id, message)
        print("✓ Sent to Telegram")
    except Exception as e:
        print(f"✗ Telegram send failed: {e}", file=sys.stderr)

    save_history(history)
    return 0


if __name__ == "__main__":
    sys.exit(main())
