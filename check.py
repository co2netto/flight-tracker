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
    # Round-trip: BKK <-> DAD targeting VZ964 outbound (08:10).
    # Return leg arrival isn't exposed in fast-flights data, so we can only filter outbound.
    {
        "origin": "BKK",
        "destination": "DAD",
        "date": "2026-05-30",
        "return_date": "2026-06-03",
        "airlines_match": ["vietjet"],
        "departure_window": ("07:30", "08:50"),
        "label_suffix": "VZ964 RT",
    },
    # One-way pair: BKK <-> DAD any airline (catches non-Vietjet alternatives)
    {"origin": "BKK", "destination": "DAD", "date": "2026-05-30"},
    {"origin": "DAD", "destination": "BKK", "date": "2026-06-03"},
    # Targeted: VZ964 BKK->DAD departing 08:10
    {
        "origin": "BKK",
        "destination": "DAD",
        "date": "2026-05-30",
        "airlines_match": ["vietjet"],
        "departure_window": ("07:30", "08:50"),
        "label_suffix": "VZ964",
    },
    # Targeted: VZ963 DAD->BKK departing 18:10
    # Note: in earlier debug, fast-flights returned empty metadata for DAD departures,
    # so this filter may exclude everything. If "no data" persists, drop the filters.
    {
        "origin": "DAD",
        "destination": "BKK",
        "date": "2026-06-03",
        "airlines_match": ["vietjet"],
        "departure_window": ("17:30", "18:50"),
        "label_suffix": "VZ963",
    },
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


def parse_time_str(s):
    """Parse a time string into minutes-since-midnight.
    Handles many formats:
      '06:30', '6:30', '6:30 AM', '15:45+1', '8:10 AM on Sat, May 30'
    Returns int or None.
    """
    if not s:
        return None
    s = str(s).strip()
    # Strip trailing date context like " on Sat, May 30"
    if " on " in s:
        s = s.split(" on ")[0].strip()
    # Strip any trailing '+1', '+2' (next-day indicators)
    s = s.split("+")[0].strip()
    # Handle "6:30 AM" / "6:30 PM"
    ampm = None
    s_upper = s.upper()
    if "AM" in s_upper:
        ampm = "AM"
        s = s_upper.replace("AM", "").strip()
    elif "PM" in s_upper:
        ampm = "PM"
        s = s_upper.replace("PM", "").strip()
    # Parse hh:mm
    if ":" not in s:
        return None
    try:
        hh, mm = s.split(":")[:2]
        h = int(hh)
        m = int(mm)
    except ValueError:
        return None
    if ampm == "PM" and h < 12:
        h += 12
    if ampm == "AM" and h == 12:
        h = 0
    return h * 60 + m


def in_time_window(time_str, window):
    """window is a (start_str, end_str) tuple like ('08:00', '12:00').
    Returns True if time_str falls inside it. None values mean no constraint on that side.
    """
    if not window:
        return True
    t = parse_time_str(time_str)
    if t is None:
        return False  # can't filter what we can't parse → exclude
    start = parse_time_str(window[0]) if window[0] else None
    end = parse_time_str(window[1]) if window[1] else None
    if start is not None and t < start:
        return False
    if end is not None and t > end:
        return False
    return True


def matches_route_filters(flight, route):
    """Apply post-fetch filters: airlines, departure_window."""
    # Airline name filter (substring match, case-insensitive)
    wanted_airlines = route.get("airlines_match")
    if wanted_airlines:
        name = str(getattr(flight, "name", "")).lower()
        if not any(a.lower() in name for a in wanted_airlines):
            return False

    # Departure time window: tuple/list of (start, end) like ("08:00", "12:00")
    dep_window = route.get("departure_window")
    if dep_window:
        if not in_time_window(getattr(flight, "departure", None), dep_window):
            return False

    # Arrival time window
    arr_window = route.get("arrival_window")
    if arr_window:
        if not in_time_window(getattr(flight, "arrival", None), arr_window):
            return False

    # Return-leg arrival window (round-trip only). Best-effort:
    # fast-flights typically exposes only outbound times, so this may exclude
    # everything if the data isn't present. We try a few likely attribute names.
    ret_arr_window = route.get("return_arrival_window")
    if ret_arr_window:
        candidates = [
            getattr(flight, "return_arrival", None),
            getattr(flight, "arrival_return", None),
        ]
        # Some libraries expose .legs or .return_flight
        legs = getattr(flight, "legs", None)
        if legs and len(legs) >= 2:
            candidates.append(getattr(legs[-1], "arrival", None))
        return_flight = getattr(flight, "return_flight", None)
        if return_flight is not None:
            candidates.append(getattr(return_flight, "arrival", None))

        # If we found any candidate, require at least one in window
        candidates = [c for c in candidates if c]
        if not candidates:
            return False  # no data → can't confirm → exclude
        if not any(in_time_window(c, ret_arr_window) for c in candidates):
            return False

    return True


def search_cheapest(route: dict):
    """
    Returns (price_int, airline_name, stops, duration_str) or (None,)*4.

    Handles one-way or round-trip based on whether route has 'return_date'.
    Post-fetch filters supported via route fields:
      - airlines_match: list of substrings to match in airline name (case-insensitive)
      - departure_window: ("HH:MM", "HH:MM") — outbound departure must fall inside
      - arrival_window:   ("HH:MM", "HH:MM") — outbound arrival must fall inside
      - return_arrival_window: ("HH:MM", "HH:MM") — return-leg arrival (round-trip only)
      - debug: True to print all matching flights' raw data
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

    try:
        result = get_flights(
            flight_data=flight_data,
            trip="round-trip" if is_roundtrip else "one-way",
            seat=SEAT,
            passengers=Passengers(adults=ADULTS, children=0, infants_in_seat=0, infants_on_lap=0),
            fetch_mode="fallback",
        )
    except Exception as e:
        print(f"  fetch error: {e}", file=sys.stderr)
        return None, None, None, None

    if not result or not getattr(result, "flights", None):
        return None, None, None, None

    # Diagnostic mode: print all candidates that match airline filter (before time filter)
    if route.get("debug"):
        print(f"  DEBUG dump for {route.get('label_suffix', 'route')}:")
        for f in result.flights:
            name = getattr(f, "name", "??")
            dep = getattr(f, "departure", "??")
            arr = getattr(f, "arrival", "??")
            dur = getattr(f, "duration", "??")
            stops_v = getattr(f, "stops", "??")
            price_v = getattr(f, "price", "??")
            wanted = route.get("airlines_match")
            tag = ""
            if wanted and any(a.lower() in str(name).lower() for a in wanted):
                tag = " <-- airline match"
            print(f"    {name} | dep={dep} arr={arr} dur={dur} stops={stops_v} price={price_v}{tag}")

    priced = []
    for f in result.flights:
        val = parse_price(getattr(f, "price", None))
        if val is None or val <= 0:
            continue
        if not matches_route_filters(f, route):
            continue
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
    suffix = route.get("label_suffix")
    suffix_tag = f" [{suffix}]" if suffix else ""
    if route.get("return_date"):
        return (
            f"{route['origin']}↔{route['destination']} "
            f"{route['date']}/{route['return_date']}{suffix_tag}"
        )
    return f"{route['origin']}→{route['destination']} {route['date']}{suffix_tag}"


def route_key(route: dict) -> str:
    suffix = route.get("label_suffix", "")
    suffix_tag = f"-{suffix}" if suffix else ""
    if route.get("return_date"):
        return f"RT-{route['origin']}-{route['destination']}-{route['date']}-{route['return_date']}{suffix_tag}"
    return f"{route['origin']}-{route['destination']}-{route['date']}{suffix_tag}"


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

    # All-time low per currently-tracked route (in the order they appear in ROUTES)
    lows_lines = []
    for route in ROUTES:
        k = route_key(route)
        entry = history.get(k)
        if not entry or "min_price" not in entry:
            continue
        min_price = entry["min_price"]
        last_price = entry.get("last_price")
        # Mark with 🔥 if the current price is at or below the all-time low
        marker = " 🔥" if last_price is not None and last_price <= min_price else ""
        # Show date the low was recorded (first 10 chars of timestamp = YYYY-MM-DD)
        when = (entry.get("min_price_at") or "")[:10]
        when_str = f" (on {when})" if when else ""
        lows_lines.append(
            f"<code>{route_label(route)}</code>  "
            f"{min_price:,} {CURRENCY_LABEL}{when_str}{marker}"
        )
    if lows_lines:
        lines.append("<b>All-time lows</b>")
        lines.extend(lows_lines)

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
