#!/usr/bin/env python3
"""
On-demand flight price check.

Run:  python check.py

Fetches real-time Google Flights prices for each configured route,
prints a summary to the terminal, and sends the same summary to Telegram.
History is kept in price_history.json so you get trend arrows vs. last run.

ROUTES supports two formats:
  One-way:    {"origin": "BKK", "destination": "ZQN", "date": "2026-07-16"}
  Round-trip: {"origin": "BKK", "destination": "DAD",
               "date": "2026-05-30", "return_date": "2026-06-03",
               "airlines": ["VZ"]}     # airlines optional, IATA codes
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
    # One-way: outbound to Queenstown
    {"origin": "BKK", "destination": "ZQN", "date": "2026-07-16"},
    {"origin": "BKK", "destination": "ZQN", "date": "2026-07-17"},
    {"origin": "BKK", "destination": "ZQN", "date": "2026-07-18"},
    {"origin": "DMK", "destination": "ZQN", "date": "2026-07-16"},
    {"origin": "DMK", "destination": "ZQN", "date": "2026-07-17"},
    {"origin": "DMK", "destination": "ZQN", "date": "2026-07-18"},
    # One-way: return from Queenstown
    {"origin": "ZQN", "destination": "BKK", "date": "2026-08-01"},
    {"origin": "ZQN", "destination": "BKK", "date": "2026-08-02"},
    # Round-trip: BKK <-> DAD (verify link to confirm VZ flights)
    {
        "origin": "BKK",
        "destination": "DAD",
        "date": "2026-05-30",
        "return_date": "2026-06-03",
    },
    # One-way pair: BKK <-> DAD any airline (catches non-Vietjet alternatives)
    {"origin": "BKK", "destination": "DAD", "date": "2026-05-30"},
    {"origin": "DAD", "destination": "BKK", "date": "2026-06-03"},
]

ADULTS = 1
SEAT = "economy"     # economy | premium-economy | business | first
CURRENCY_LABEL = "THB"  # Google Flights returns prices in your locale's currency

HISTORY_FILE = Path("price_history.json")


# ---------- Google Flights (via fast-flights) ----------

def parse_price(p):
    if not p:
        return None
    digits = "".join(ch for ch in str(p) if ch.isdigit())
    return int(digits) if digits else None


def search_cheapest(route: dict):
    """
    Returns (price_int, airline_name, stops, duration_str) or (None,)*4.

    Handles one-way or round-trip based on whether route has 'return_date'.
    Optionally filters by airline IATA codes.
    """
    is_roundtrip = bool(route.get("return_date"))
    flight_data = [FlightData(
        date=route["date"],
        from_airport=route["origin"],
        to_airport=route["destination"],
    )]
    if is_roundtrip:
        flight_data.append(FlightData(
            date=route["return_date"],
            from_airport=route["destination"],
            to_airport=route["origin"],
        ))

    kwargs = {
        "flight_data": flight_data,
        "trip": "round-trip" if is_roundtrip else "one-way",
        "seat": SEAT,
        "passengers": Passengers(adults=ADULTS, children=0, infants_in_seat=0, infants_on_lap=0),
        "fetch_mode": "fallback",
    }

    # Optional airline filter
    airlines = route.get("airlines")
    if airlines:
        # Library accepts a list of IATA codes via this parameter; ignored if unsupported
        kwargs["airlines"] = airlines

    try:
        result = get_flights(**kwargs)
    except TypeError:
        # In case 'airlines' kwarg is rejected by this version of fast-flights, retry without it
        kwargs.pop("airlines", None)
        try:
            result = get_flights(**kwargs)
        except Exception as e:
            print(f"  fetch error: {e}", file=sys.stderr)
            return None, None, None, None
    except Exception as e:
        print(f"  fetch error: {e}", file=sys.stderr)
        return None, None, None, None

    if not result or not getattr(result, "flights", None):
        return None, None, None, None

    priced = []
    for f in result.flights:
        val = parse_price(getattr(f, "price", None))
        if val is not None and val > 0:
            priced.append((val, f))
    if not priced:
        return None, None, None, None

    priced.sort(key=lambda x: x[0])
    price, flight = priced[0]
    airline = getattr(flight, "name", "??") or "??"
    stops = getattr(flight, "stops", 0) or 0
    duration = getattr(flight, "duration", "") or ""
    return price, airline, stops, duration


# ---------- Helpers ----------

def format_stops(stops):
    """Normalize fast-flights' varied stop formats into readable text."""
    s = str(stops).lower().strip()
    if s in ("0", "", "none", "nonstop", "non-stop", "direct"):
        return "direct"
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits:
        n = int(digits)
        return f"{n} stop" + ("s" if n > 1 else "")
    if s == "unknown":
        return None
    return s


def google_flights_url(route: dict) -> str:
    """Direct Google Flights search URL (one-way or round-trip)."""
    origin = route["origin"]
    destination = route["destination"]
    date = route["date"]
    if route.get("return_date"):
        return (
            "https://www.google.com/travel/flights"
            f"?q=Flights%20to%20{destination}%20from%20{origin}%20on%20{date}"
            f"%20returning%20{route['return_date']}"
        )
    return (
        "https://www.google.com/travel/flights"
        f"?q=Flights%20to%20{destination}%20from%20{origin}%20on%20{date}%20oneway"
    )


def trend_marker(current: float, previous):
    if previous is None:
        return "🆕"
    if current < previous:
        return f"🟢↓ -{previous - current:.0f}"
    if current > previous:
        return f"🔴↑ +{current - previous:.0f}"
    return "⚪→"


def route_label(route: dict) -> str:
    if route.get("return_date"):
        airline_tag = ""
        if route.get("airlines"):
            airline_tag = f" [{'/'.join(route['airlines'])}]"
        return (
            f"{route['origin']}↔{route['destination']} "
            f"{route['date']}/{route['return_date']}{airline_tag}"
        )
    return f"{route['origin']}→{route['destination']} {route['date']}"


def route_key(route: dict) -> str:
    if route.get("return_date"):
        a = "+".join(route.get("airlines", []))
        suffix = f"-{a}" if a else ""
        return f"RT-{route['origin']}-{route['destination']}-{route['date']}-{route['return_date']}{suffix}"
    return f"{route['origin']}-{route['destination']}-{route['date']}"


def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {}


def save_history(history: dict) -> None:
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


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
    outbound_lines = []
    return_lines = []
    rt_lines = []
    other_lines = []

    for route in ROUTES:
        k = route_key(route)
        label = route_label(route)
        print(f"Checking {label}…")
        prev = history.get(k, {}).get("last_price")
        price, airline, stops, duration = search_cheapest(route)

        if price is None:
            line = (
                f'<a href="{google_flights_url(route)}">{label}</a>  no data'
            )
        else:
            marker = trend_marker(price, prev)
            stop_txt = format_stops(stops)
            airline_clean = airline if airline and airline != "??" else None

            details_parts = []
            if airline_clean:
                details_parts.append(airline_clean[:25])
            if stop_txt:
                details_parts.append(stop_txt)
            if duration:
                details_parts.append(duration)

            if details_parts:
                details = " (" + ", ".join(details_parts) + ")"
            else:
                details = " <i>(details unavailable)</i>"

            url = google_flights_url(route)
            line = (
                f'<a href="{url}">{label}</a>  '
                f"<b>{price:,} {CURRENCY_LABEL}</b>{details} {marker}"
            )

            entry = history.get(k, {"history": []})
            entry["last_price"] = price
            entry["last_checked"] = ts
            entry["history"] = (entry.get("history", []) + [{"ts": ts, "price": price}])[-100:]
            if "min_price" not in entry or price < entry["min_price"]:
                entry["min_price"] = price
                entry["min_price_at"] = ts
            history[k] = entry

        # Categorize for output grouping
        if route.get("return_date"):
            rt_lines.append(line)
        elif route["destination"] == "ZQN":
            outbound_lines.append(line)
        elif route["origin"] == "ZQN":
            return_lines.append(line)
        else:
            other_lines.append(line)

        time.sleep(1)

    if outbound_lines:
        lines.append("<b>Outbound → ZQN</b>")
        lines.extend(outbound_lines)
        lines.append("")
    if return_lines:
        lines.append("<b>Return ← BKK</b>")
        lines.extend(return_lines)
        lines.append("")
    if other_lines:
        lines.append("<b>Other one-way</b>")
        lines.extend(other_lines)
        lines.append("")
    if rt_lines:
        lines.append("<b>Round-trip</b>")
        lines.extend(rt_lines)
        lines.append("")

    lows = [(k, v["min_price"]) for k, v in history.items() if "min_price" in v]
    if lows:
        lows.sort(key=lambda x: x[1])
        lines.append("<b>All-time lows (top 3)</b>")
        for k, p in lows[:3]:
            lines.append(f"<code>{k}</code>  {p:,} {CURRENCY_LABEL}")

    message = "\n".join(lines)

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
