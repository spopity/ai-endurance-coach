"""Keeps the resting HR in the Intervals.icu profile steady and close to reality.

Intervals.icu scores every session without power (runs, gym, walks) from heart rate, and that
score depends on the profile's resting HR. Left to itself, Intervals takes each new value from
the Garmin wellness download - often a provisional 55-59 overnight, ~50 once the watch's night
arrives - so the loads, fatigue and form were re-scored several times a day.

Instead, twice a week (SET_DAYS) the bot sets it to the median of the watch's last 7 resting HRs
(daily_metrics, from Garmin directly), and only when that differs from what Intervals has.
The last value set is kept in state/resting_hr_set.json; readiness.night_data() treats a profile
still holding it as settled.
"""
import json
import logging
import os
from datetime import date, datetime, timedelta
from statistics import median

from core.overview import query
from core.schedule import TZ

logger = logging.getLogger("AI_COACH_V1.resting_hr")

STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state",
                          "resting_hr_set.json")
SET_DAYS = (0, 3)        # Monday, Thursday
WINDOW_DAYS = 7
MIN_VALUES = 4           # fewer watch readings than this in the window: leave it alone


def last_set() -> dict | None:
    """{date, value, previous, values} of the last write, or None."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def target(today: date) -> tuple[int | None, list[int]]:
    """(median of the watch's resting HR over the WINDOW_DAYS before today, the values used)."""
    rows = query("SELECT resting_hr FROM daily_metrics WHERE day > %s AND day <= %s AND resting_hr IS NOT NULL "
                 "ORDER BY day;", (today - timedelta(days=WINDOW_DAYS), today))
    values = [int(r["resting_hr"]) for r in rows]
    return (round(median(values)) if len(values) >= MIN_VALUES else None), values


def _api(method: str, **kw):
    from core.workouts import _api as intervals_api   # same Intervals.icu athlete endpoint
    return intervals_api(method, "", **kw)


def maybe_set(today: date, force: bool = False) -> dict | None:
    """On SET_DAYS (or force): sets the Intervals.icu resting HR to the 7-day median if it differs.
    Returns what was written, or None when nothing was."""
    if not force and today.weekday() not in SET_DAYS:
        return None
    value, values = target(today)
    if value is None:
        logger.info(f"Resting HR not set: only {len(values)} watch readings in {WINDOW_DAYS} days.")
        return None
    current = _api("GET").json().get("icu_resting_hr")
    if current != value:
        _api("PUT", json={"icu_resting_hr": value}).raise_for_status()
        logger.info(f"Intervals.icu resting HR {current} -> {value} (7-day median of {values}).")
    entry = {"date": today.isoformat(), "value": value, "previous": current, "values": values,
             "at": datetime.now(TZ).isoformat(timespec="minutes")}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(entry, f)
    os.replace(tmp, STATE_FILE)   # the next sync pulls the new value into athlete_profile
    return entry if current != value else None
