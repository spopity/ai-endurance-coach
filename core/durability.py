"""
Durability: how well the athlete holds power and heart rate when already tired - in the 2024-25
research a performance factor of its own, independent of VO2max and FTP (successful amateurs
lose about half as much power after ~1000 kJ). Numbers only, two markers:

- Aerobic durability: heart-rate drift (Intervals.icu Pa:HR decoupling) on steady long rides -
  90+ min, variability index <= STEADY_VI, IF in the endurance band. Friel's measure: under
  5% means the aerobic base is established (core/periodization.py uses it to unlock Build).
  Hilly or stop-start rides are skipped: their drift is noise.
- Late power: the best 20-min (and 5-min) power after LATE_KJ_PER_KG of work in a ride, from the
  power stream (Intervals.icu, cached per activity in state/durability.json), as a share of the
  athlete's training FTP that day. It only counts when the late effort was a real one (>=
  LATE_MIN_SHARE of FTP): a long ride ridden easy to the end says nothing about durability.

The plan trains it (core/schedule.py): in Build the long ride finishes with efforts while tired,
and every DURABILITY_TEST_EVERY_DAYS it becomes a durability test - Zone 2 until LATE_KJ_PER_KG,
then 20 min all-out.
"""
import json
import logging
import os
import statistics
from datetime import date

import requests

from core.db import ROOT, load_env

logger = logging.getLogger("AI_COACH_V1.durability")

CACHE_FILE = os.path.join(ROOT, "state", "durability.json")
STEADY_MIN_MINUTES = 90
STEADY_VI = 1.10
STEADY_IF = (0.55, 0.80)
DECOUPLING_OK = 5.0          # % - Friel: under this the aerobic base is established
DECOUPLING_PLAUSIBLE = 15.0  # beyond +-this the HR or power data is broken, not the athlete
LATE_KJ_PER_KG = 15          # work before the "late" efforts count (~1000 kJ for 70 kg)
LATE_MIN_SHARE = 0.80        # a late 20-min effort under this share of FTP wasn't a real effort
DURABILITY_TEST_EVERY_DAYS = 42


def is_steady(minutes, vi, intensity) -> bool:
    return (bool(minutes and vi and intensity) and minutes >= STEADY_MIN_MINUTES and vi <= STEADY_VI
            and STEADY_IF[0] <= intensity <= STEADY_IF[1])


def steady_decoupling(rides: list[dict]) -> list[tuple[date, float]]:
    """[(day, decoupling %)] of the steady long rides among `rides` (each: day, moving_time_sec,
    variability_index, intensity_factor, decoupling_pct), oldest first."""
    out = []
    for r in rides:
        dec = r.get("decoupling_pct")
        if (dec is not None and abs(dec) <= DECOUPLING_PLAUSIBLE
                and is_steady((r.get("moving_time_sec") or 0) / 60, r.get("variability_index"), r.get("intensity_factor"))):
            out.append((r["day"], round(float(dec), 1)))
    return out


def aerobic_ready(steady: list[tuple[date, float]], on: date, days: int = 21, rides: int = 2) -> bool:
    """The last `rides` steady long rides within `days` before `on` all drifted under 5%."""
    recent = [dec for d, dec in steady if 0 <= (on - d).days < days]
    return len(recent) >= rides and all(abs(dec) < DECOUPLING_OK for dec in recent[-rides:])


# --- late power from the stream ---

def best_after(watts: list, weight_kg: float, kj_per_kg: float = LATE_KJ_PER_KG,
               durations: tuple = (300, 1200)) -> dict | None:
    """{secs: best average watts over `secs` starting after kj_per_kg x weight of work} from a
    1-second power stream; None when the ride never got there."""
    threshold = kj_per_kg * weight_kg * 1000
    total, start = 0.0, None
    w = [float(x or 0) for x in watts]
    for i, x in enumerate(w):
        total += x
        if total >= threshold:
            start = i + 1
            break
    if start is None:
        return None
    out = {}
    late = w[start:]
    for secs in durations:
        if len(late) < secs:
            continue
        s = sum(late[:secs])
        best = s
        for i in range(secs, len(late)):
            s += late[i] - late[i - secs]
            best = max(best, s)
        out[secs] = round(best / secs)
    return {"after_kj": round(threshold / 1000), **{f"p{secs // 60}": v for secs, v in out.items()}}


def _cache() -> dict:
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_FILE)


def _stream(external_id: str) -> list:
    env = load_env()
    r = requests.get(f"https://intervals.icu/api/v1/activity/{external_id}/streams.json", params={"types": "watts"},
                     auth=("API_KEY", env["INTERVALS_API_KEY"]), timeout=60)
    r.raise_for_status()
    return next((s.get("data") or [] for s in r.json() if s.get("type") == "watts"), [])


def late_power(rides: list[dict], weight_kg: float | None, fetch: bool = True) -> dict[str, dict]:
    """{external_id: {after_kj, p5, p20}} for the rides with enough work (each: external_id,
    joules). Streams are fetched once and cached; `fetch=False` only reads the cache."""
    if not weight_kg:
        return {}
    cache, changed, out = _cache(), False, {}
    need = LATE_KJ_PER_KG * weight_kg * 1000
    for r in rides:
        eid = r.get("external_id")
        if not eid or not r.get("joules") or r["joules"] < need + 300 * 150:   # room for a late effort
            continue
        key = f"{eid}:{LATE_KJ_PER_KG}:{round(weight_kg)}"
        if key not in cache and fetch:
            try:
                cache[key] = best_after(_stream(eid), weight_kg) or {}
                changed = True
            except Exception as e:
                logger.warning(f"Power stream unavailable for {eid}: {e}")
                continue
        if cache.get(key):
            out[eid] = cache[key]
    if changed:
        _save_cache(cache)
    return out


def late_share(late: dict, ftp: float | None) -> float | None:
    """Best late 20-min power as a share of FTP, when it was a real effort."""
    p20 = (late or {}).get("p20")
    if not p20 or not ftp:
        return None
    share = p20 / ftp
    return round(share, 3) if share >= LATE_MIN_SHARE else None


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None
