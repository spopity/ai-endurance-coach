"""
Races: upcoming ones from the athlete's Intervals.icu calendar (events with
category RACE_A / RACE_B / RACE_C) and past ones from the activities table.

Race details are read from the event's own fields where Intervals.icu has them
(type, date, planned distance) and otherwise parsed from the event description,
which is free text. Recommended description format (any order, any line):

    Distance: 120 km
    Elevation: 1500 m
    Location: Banska Bystrica, Slovakia
    Goal: finish under 5h
"""
import logging
import re
from datetime import date, datetime, timedelta

import requests

from core.db import get_connection, load_env

logger = logging.getLogger("AI_COACH_V1.races")

BASE_URL = "https://intervals.icu"
RACE_CATEGORIES = {"RACE_A": "A", "RACE_B": "B", "RACE_C": "C"}

_DISTANCE_RE = re.compile(r"(?:distance|dist)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*(km|mi|k)?", re.I)
_KM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*km\b", re.I)
_ELEVATION_RE = re.compile(
    r"(?:elevation|elev|climbing|ascent|gain|vert(?:ical)?|d\+)\s*[:=]?\s*(\d[\d,. ]*)\s*(?:m|hm)?\b"
    r"|(\d[\d,.]*)\s*(?:m|hm)\s*(?:elevation|of climbing|climbing|ascent|gain|d\+|vert)"
    r"|(\d[\d,.]*)\s*hm\b",  # "hm" (Hoehenmeter) is unambiguous on its own
    re.I,
)
_LOCATION_RE = re.compile(r"(?:location|place|where|venue|start)\s*[:=]\s*([^\n;]+)", re.I)
_GOAL_RE = re.compile(r"(?:goal|target|aim)\s*[:=]\s*([^\n;]+)", re.I)
_SURFACE_HINTS = (("gravel", "GravelRide"), ("mtb", "MountainBikeRide"), ("mountain bike", "MountainBikeRide"),
                  ("xco", "MountainBikeRide"), ("marathon", "MountainBikeRide"), ("road", "Ride"))


def _num(s: str) -> float:
    return float(s.replace(" ", "").replace(",", "."))


def parse_race_text(text: str) -> dict:
    """Pulls distance (km), elevation (m), location and goal out of free text."""
    out = {}
    if not text:
        return out
    m = _DISTANCE_RE.search(text) or _KM_RE.search(text)
    if m:
        value = _num(m.group(1))
        unit = (m.group(2) or "km").lower() if m.re is _DISTANCE_RE else "km"
        out["distance_km"] = value * 1.609 if unit == "mi" else value
    m = _ELEVATION_RE.search(text)
    if m:
        raw = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        # "1,500" / "1.500" as a thousands separator -> 1500
        cleaned = re.sub(r"[ ,.](?=\d{3}\b)", "", raw)
        try:
            out["elevation_m"] = _num(cleaned)
        except ValueError:
            pass
    m = _LOCATION_RE.search(text)
    if m:
        out["location"] = m.group(1).strip()
    m = _GOAL_RE.search(text)
    if m:
        out["goal"] = m.group(1).strip()
    return out


def _event_to_race(e: dict) -> dict:
    text = f"{e.get('name') or ''}\n{e.get('description') or ''}"
    parsed = parse_race_text(e.get("description") or "")
    if "distance_km" not in parsed:
        parsed.update({k: v for k, v in parse_race_text(e.get("name") or "").items() if k == "distance_km"})
    distance_km = parsed.get("distance_km")
    if not distance_km and e.get("distance"):
        d = float(e["distance"])
        distance_km = d / 1000 if d > 1000 else d
    race_type = e.get("type")
    if not race_type or race_type == "Ride":
        lowered = text.lower()
        for hint, t in _SURFACE_HINTS:
            if hint in lowered:
                race_type = t
                break
    start = str(e.get("start_date_local") or "")[:10]
    return {
        "id": e.get("id"),
        "name": e.get("name") or "Unnamed race",
        "date": start,
        "days_until": (date.fromisoformat(start) - date.today()).days if start else None,
        "priority": RACE_CATEGORIES.get(e.get("category"), "?"),
        "type": race_type or "Ride",
        "distance_km": round(distance_km, 1) if distance_km else None,
        "elevation_m": parsed.get("elevation_m"),
        "location": parsed.get("location"),
        "goal": parsed.get("goal"),
        "description": e.get("description") or "",
        "missing": [k for k, v in (("distance", distance_km), ("elevation", parsed.get("elevation_m")),
                                   ("location", parsed.get("location"))) if not v],
    }


def fetch_upcoming_races(days_ahead: int = 365) -> list[dict]:
    """Race events (A/B/C) from today forward, soonest first."""
    env = load_env()
    resp = requests.get(
        f"{BASE_URL}/api/v1/athlete/{env['INTERVALS_ATHLETE_ID']}/events",
        params={"oldest": date.today().isoformat(), "newest": (date.today() + timedelta(days=days_ahead)).isoformat()},
        auth=("API_KEY", env["INTERVALS_API_KEY"]),
        timeout=30,
    )
    resp.raise_for_status()
    races = [_event_to_race(e) for e in resp.json() if e.get("category") in RACE_CATEGORIES]
    return sorted(races, key=lambda r: r["date"])


def list_upcoming_races() -> str:
    """Lists the athlete's upcoming races from their Intervals.icu calendar (Race A/B/C events), with
    date, priority, type, distance, elevation and location, and which details are missing."""
    try:
        races = fetch_upcoming_races()
    except Exception as e:
        logger.error(f"Fetching races failed: {e}", exc_info=True)
        return f"Could not read the Intervals.icu calendar: {e}"
    if not races:
        return ("No upcoming races in the Intervals.icu calendar. The athlete can add one as a Race A/B/C event, "
                "with 'Distance: X km', 'Elevation: Y m' and 'Location: Z' in the description - or give the "
                "details directly in chat.")
    lines = ["Upcoming races (Intervals.icu calendar):"]
    for r in races:
        details = ", ".join(filter(None, [
            r["type"],
            f"{r['distance_km']:g} km" if r["distance_km"] else None,
            f"{r['elevation_m']:.0f} m elevation" if r["elevation_m"] else None,
            r["location"],
            f"goal: {r['goal']}" if r["goal"] else None,
        ]))
        line = f"- [{r['id']}] {r['date']} (in {r['days_until']} days) - {r['priority']} race: {r['name']} - {details}"
        if r["missing"]:
            line += f" (missing: {', '.join(r['missing'])})"
        lines.append(line)
    return "\n".join(lines)


def past_races(limit: int = 10) -> list[dict]:
    """Activities flagged as races, newest first, with the key pacing numbers."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT external_id, start_time, type, name, distance_m, moving_time_sec, total_elevation_gain_m,
                       avg_power_w, normalized_power_w, intensity_factor, avg_hr, max_hr, lthr, ftp_at_time,
                       decoupling_pct, training_load, raw_payload->>'average_temp' AS avg_temp
                FROM activities WHERE race AND superseded_by IS NULL
                ORDER BY start_time DESC LIMIT %s;
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def format_past_races(races: list[dict]) -> str:
    if not races:
        return "No past activities are flagged as races."
    lines = ["Past races (activities flagged as race):"]
    for r in races:
        parts = [f"{r['type']}", f"{(r['distance_m'] or 0) / 1000:.0f} km"]
        if r.get("total_elevation_gain_m"):
            parts.append(f"{r['total_elevation_gain_m']:.0f} m up")
        if r.get("moving_time_sec"):
            h, m = divmod(int(r["moving_time_sec"]) // 60, 60)
            parts.append(f"{h}h{m:02d}")
        if r.get("normalized_power_w"):
            parts.append(f"NP {r['normalized_power_w']:.0f} W")
        if r.get("intensity_factor"):
            parts.append(f"IF {r['intensity_factor']:.2f}")
        if r.get("avg_hr") and r.get("lthr"):
            parts.append(f"avg HR {r['avg_hr']:.0f} ({r['avg_hr'] / r['lthr'] * 100:.0f}% LTHR)")
        if r.get("decoupling_pct") is not None:
            parts.append(f"decoupling {r['decoupling_pct']:.1f}%")
        if r.get("avg_temp"):
            parts.append(f"{float(r['avg_temp']):.0f}°C")
        start = r["start_time"].date() if isinstance(r["start_time"], datetime) else r["start_time"]
        lines.append(f"- {start} {r['name']} [{r['external_id']}]: " + ", ".join(parts))
    return "\n".join(lines)
