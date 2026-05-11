#!/usr/bin/env python3
"""
On-demand flight price check — fli library version.

Run:  python check_fli.py

This is a PARALLEL implementation alongside check.py. It uses the `fli` library
(direct Google Flights API access) instead of `fast-flights` (HTML scraping).
Writes to a separate history file (price_history_fli.json) so it doesn't
interfere with the main tracker.

Benefits over fast-flights:
- More reliable metadata (airline names, times always populated)
- Per-leg details including flight_number for exact matching
- Proper airline/stops/time filters via API parameters
- More stable (API contract vs. HTML scraping)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from fli.models import (
    Airport,
    FlightSearchFilters,
    FlightSegment,
    MaxStops,
    PassengerInfo,
    SeatType,
    SortBy,
    TripType,
)
from fli.search import SearchFlights


# ---------- Configuration ----------

# Routes are loaded from routes.json (shared with check.py).
# Edit routes.json to add/remove/reorder flights — no need to touch this file.

ROUTES_FILE = Path(__file__).parent / "routes.json"


def load_routes():
    """Load route definitions from routes.json.
    Returns a list mixing section-marker dicts and route dicts, preserving order.
    Section markers have key '_section' and a string value.
    Route entries have 'origin'/'destination'/'date'.
    """
    if not ROUTES_FILE.exists():
        print(f"ERROR: {ROUTES_FILE} not found", file=sys.stderr)
        sys.exit(1)
    with open(ROUTES_FILE) as f:
        data = json.load(f)
    raw = data.get("routes", [])
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if "_section" in entry:
            out.append({"_section": entry["_section"]})
            continue
        if "origin" not in entry:
            continue  # other comments like format reference
        for key in ("departure_window", "arrival_window", "return_departure_window"):
            if key in entry and isinstance(entry[key], list):
                entry[key] = tuple(entry[key])
        out.append(entry)
    return out


ROUTES_AND_SECTIONS = load_routes()
ROUTES = [r for r in ROUTES_AND_SECTIONS if "_section" not in r]

ADULTS = 1
CURRENCY_LABEL = "THB"  # fli typically returns USD; we display whatever the API gives

HISTORY_FILE = Path("price_history_fli.json")


# ---------- Helpers ----------

def airport_enum(code: str):
    """Map IATA code to fli's Airport enum."""
    try:
        return getattr(Airport, code.upper())
    except AttributeError:
        raise ValueError(f"Airport code '{code}' not in fli's Airport enum")


def parse_time_str(s):
    """'HH:MM' -> minutes since midnight."""
    if not s:
        return None
    try:
        hh, mm = s.split(":")[:2]
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def in_window(dt, window):
    """Check if a datetime falls within the (start_str, end_str) window."""
    if not window or not dt:
        return True
    try:
        t_minutes = dt.hour * 60 + dt.minute
    except AttributeError:
        return False
    start = parse_time_str(window[0]) if window[0] else None
    end = parse_time_str(window[1]) if window[1] else None
    if start is not None and t_minutes < start:
        return False
    if end is not None and t_minutes > end:
        return False
    return True


def flight_number_matches(flight, wanted: str) -> bool:
    """Check if the first leg's flight number matches wanted (e.g. 'VZ964')."""
    if not flight or not getattr(flight, "legs", None):
        return False
    leg = flight.legs[0]
    airline_code = getattr(getattr(leg, "airline", None), "value", "")
    flight_num = getattr(leg, "flight_number", "")
    combined = f"{airline_code}{flight_num}".upper().replace(" ", "")
    return combined == wanted.upper().replace(" ", "")


def search_cheapest(route: dict):
    """
    Returns a dict with flight info, or None if no results.
    """
    is_roundtrip = bool(route.get("return_date"))

    try:
        segments = [
            FlightSegment(
                departure_airport=[[airport_enum(route["origin"]), 0]],
                arrival_airport=[[airport_enum(route["destination"]), 0]],
                travel_date=route["date"],
            )
        ]
        if is_roundtrip:
            segments.append(
                FlightSegment(
                    departure_airport=[[airport_enum(route["destination"]), 0]],
                    arrival_airport=[[airport_enum(route["origin"]), 0]],
                    travel_date=route["return_date"],
                )
            )

        filters = FlightSearchFilters(
            trip_type=TripType.ROUND_TRIP if is_roundtrip else TripType.ONE_WAY,
            passenger_info=PassengerInfo(adults=ADULTS),
            flight_segments=segments,
            seat_type=SeatType.ECONOMY,
            stops=MaxStops.ANY,
            sort_by=SortBy.CHEAPEST,
        )

        search = SearchFlights()
        results = search.search(filters)
    except Exception as e:
        print(f"  fetch error: {e}", file=sys.stderr)
        return None

    if not results:
        return None

    # Normalize: round-trip returns list of (outbound, return) tuples;
    # one-way returns list of Flight objects.
    candidates = []
    for r in results:
        if is_roundtrip:
            outbound, ret = r
            # fli returns the SAME round-trip price on both outbound and return objects
            # (it's the total RT price, not per-leg). Use outbound's price directly.
            total_price = getattr(outbound, "price", 0) or 0
            candidates.append((total_price, outbound, ret))
        else:
            candidates.append((getattr(r, "price", 0) or 0, r, None))

    # Apply flight_number filter
    wanted_flight = route.get("flight_number")
    if wanted_flight:
        candidates = [c for c in candidates if flight_number_matches(c[1], wanted_flight)]

    # Apply departure_window filter (on outbound leg's first departure)
    dep_window = route.get("departure_window")
    if dep_window:
        def in_dep_window(c):
            _price, flight, _ret = c
            legs = getattr(flight, "legs", None)
            if not legs:
                return False
            return in_window(getattr(legs[0], "departure_datetime", None), dep_window)
        candidates = [c for c in candidates if in_dep_window(c)]

    # Apply return_departure_window filter (round-trip only; uses return flight's first leg)
    ret_dep_window = route.get("return_departure_window")
    if ret_dep_window:
        def in_ret_dep_window(c):
            _price, _outbound, ret_flight = c
            if ret_flight is None:
                return False
            ret_legs = getattr(ret_flight, "legs", None)
            if not ret_legs:
                return False
            return in_window(getattr(ret_legs[0], "departure_datetime", None), ret_dep_window)
        candidates = [c for c in candidates if in_ret_dep_window(c)]

    # Filter out zero prices
    candidates = [c for c in candidates if c[0] and c[0] > 0]

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    price, flight, ret_flight = candidates[0]

    def extract_leg_info(flight_obj):
        if not flight_obj:
            return "", "", ""
        legs = getattr(flight_obj, "legs", None)
        if not legs:
            return "", "", ""
        leg0 = legs[0]
        airline_obj = getattr(leg0, "airline", None)
        airline = getattr(airline_obj, "name", None) or getattr(airline_obj, "value", "") or ""
        fno = getattr(leg0, "flight_number", "") or ""
        dep_dt = getattr(leg0, "departure_datetime", None)
        dep = ""
        if dep_dt:
            try:
                dep = dep_dt.strftime("%H:%M")
            except AttributeError:
                dep = str(dep_dt)
        return airline, fno, dep

    airline_name, flight_no, dep_time = extract_leg_info(flight)
    ret_airline, ret_flight_no, ret_dep_time = extract_leg_info(ret_flight)

    stops = getattr(flight, "stops", 0) or 0
    duration_min = getattr(flight, "duration", 0) or 0

    return {
        "price": price,
        "airline": airline_name,
        "flight_no": flight_no,
        "dep_time": dep_time,
        "stops": stops,
        "duration_min": duration_min,
        "ret_airline": ret_airline,
        "ret_flight_no": ret_flight_no,
        "ret_dep_time": ret_dep_time,
    }


# ---------- History ----------

def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {}


def save_history(history: dict) -> None:
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


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


def google_flights_url(route: dict) -> str:
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


def format_duration(minutes: int) -> str:
    if not minutes:
        return ""
    h, m = divmod(int(minutes), 60)
    parts = []
    if h:
        parts.append(f"{h} hr")
    if m:
        parts.append(f"{m} min")
    return " ".join(parts)


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

    lines = [f"<b>🔬 Flight check (fli) — {ts}</b>", ""]

    # Walk routes.json entries in order. Emit a header for each {_section: "..."} marker,
    # then the result line for each real route. This makes the Telegram output mirror
    # the structure of routes.json so you can reorder/group there.
    for entry in ROUTES_AND_SECTIONS:
        if "_section" in entry:
            lines.append("")
            lines.append(f"<b>{entry['_section']}</b>")
            continue

        route = entry
        k = route_key(route)
        label = route_label(route)
        print(f"Checking {label}…")
        prev = history.get(k, {}).get("last_price")
        info = search_cheapest(route)

        if info is None:
            line = f'<a href="{google_flights_url(route)}">{label}</a>  no data'
        else:
            price = info["price"]
            marker = trend_marker(price, prev)
            details_parts = []
            if info["airline"]:
                details_parts.append(info["airline"][:25])
            if info["flight_no"]:
                details_parts.append(f"#{info['flight_no']}")
            if info["dep_time"]:
                details_parts.append(f"dep {info['dep_time']}")
            stops_int = int(info["stops"]) if isinstance(info["stops"], (int, float)) else 0
            if stops_int == 0:
                details_parts.append("direct")
            else:
                details_parts.append(f"{stops_int} stop{'s' if stops_int > 1 else ''}")
            dur_str = format_duration(info["duration_min"])
            if dur_str:
                details_parts.append(dur_str)

            # For round-trips, append return-leg info on a second sub-line
            ret_info = ""
            if route.get("return_date") and info.get("ret_flight_no"):
                ret_parts = []
                if info.get("ret_airline"):
                    ret_parts.append(info["ret_airline"][:25])
                ret_parts.append(f"#{info['ret_flight_no']}")
                if info.get("ret_dep_time"):
                    ret_parts.append(f"dep {info['ret_dep_time']}")
                ret_info = f"\n    ↩️ return: " + ", ".join(ret_parts)

            details = " (" + ", ".join(details_parts) + ")" if details_parts else ""
            url = google_flights_url(route)
            line = (
                f'<a href="{url}">{label}</a>  '
                f"<b>{price:,.0f} {CURRENCY_LABEL}</b>{details} {marker}{ret_info}"
            )

            entry_hist = history.get(k, {"history": []})
            entry_hist["last_price"] = price
            entry_hist["last_checked"] = ts
            entry_hist["history"] = (entry_hist.get("history", []) + [{"ts": ts, "price": price}])[-100:]
            if "min_price" not in entry_hist or price < entry_hist["min_price"]:
                entry_hist["min_price"] = price
                entry_hist["min_price_at"] = ts
            history[k] = entry_hist

        lines.append(line)
        time.sleep(1)

    lines.append("")

    # All-time lows per tracked route
    lows_lines = []
    for route in ROUTES:
        k = route_key(route)
        entry = history.get(k)
        if not entry or "min_price" not in entry:
            continue
        min_price = entry["min_price"]
        last_price = entry.get("last_price")
        marker = " 🔥" if last_price is not None and last_price <= min_price else ""
        when = (entry.get("min_price_at") or "")[:10]
        when_str = f" (on {when})" if when else ""
        lows_lines.append(
            f"<code>{route_label(route)}</code>  "
            f"{min_price:,.0f} {CURRENCY_LABEL}{when_str}{marker}"
        )
    if lows_lines:
        lines.append("<b>All-time lows (fli)</b>")
        lines.extend(lows_lines)

    message = "\n".join(lines)

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
