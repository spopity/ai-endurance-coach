"""
Two load streams instead of one. Training load (TSS, Intervals.icu's CTL/ATL) measures the
cardio cost of every session alike, but 50 TSS of running and 50 TSS of riding are not the same
stress for bones, tendons and muscles: running (and leg day) loads the tissue, riding hardly does.
In a dual-sport week the cardio ratio can look calm while the legs take a spike - the ride TSS
hides it. So the impact load is tracked on its own, in km-equivalents (km-eq):

- run: km x (1 + share of time in HR zone 4+), plus 1 km-eq per 100 m climbed (hills load the
  calves and, on the way down, the quads and shins),
- leg gym session (core/gym.LEG_WORDS in the title): session RPE x minutes / 30 (RPE 6 when not
  given) - a hard 45-min leg day ~ 10 km-eq,
- walk/hike: a quarter of the run value; rides and upper-body gym: 0.

Acute (7-day) and chronic (28-day) exponentially weighted averages (Williams et al. 2017: EWMA
reacts to spikes better than rolling sums); their ratio is the impact ACWR. Spikes in single
runs (core/injury_checks.check_run_spikes) stay the main running-injury signal (Frandsen et al.,
BJSM 2025); this adds the cumulative view across running, hills and leg day.
"""
import re
from collections import defaultdict
from datetime import date, timedelta

from core.gym import LEG_WORDS, WEIGHTS_TYPES

RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
FOOT_TYPES = {"Walk": 0.25, "Hike": 0.25}
GYM_DEFAULT_RPE = 6
CLIMB_M_PER_KM = 100
ACUTE_DAYS, CHRONIC_DAYS = 7, 28
MIN_CHRONIC_WEEK = 10.0    # km-eq/week: below this the base is too small for a ratio (low-base rule instead)
IMPACT_CAUTION = 1.5


def _hard_share(hz) -> float:
    if isinstance(hz, str):
        hz = [int(x) for x in re.findall(r"\d+", hz)]
    total = sum(hz or [])
    return sum((hz or [])[3:]) / total if total else 0.0


def impact(a: dict) -> float:
    """km-eq of one activity (type, name, distance_m, moving_time_sec, gain, rpe, hr_zone_times)."""
    t = a.get("type")
    km = float(a.get("distance_m") or 0) / 1000
    climb = float(a.get("gain") or 0) / CLIMB_M_PER_KM
    if t in RUN_TYPES:
        return km * (1 + _hard_share(a.get("hr_zone_times"))) + climb
    if t in FOOT_TYPES:
        return FOOT_TYPES[t] * (km + climb)
    if t in WEIGHTS_TYPES and LEG_WORDS.search(a.get("name") or ""):
        return (a.get("rpe") or GYM_DEFAULT_RPE) * (a.get("moving_time_sec") or 0) / 60 / 30
    return 0.0


def ewma(values: list[float], days: int) -> float:
    lam, out = 2 / (days + 1), 0.0
    for v in values:
        out = lam * v + (1 - lam) * out
    return out


def streams(activities: list[dict], today: date, ctl: float | None, atl: float | None,
            lookback: int = 150) -> dict:
    """Impact load (acute/chronic per week, ratio, last 7 days by source) next to the cardio
    ratio (ATL/CTL). `activities` need a `day`."""
    start = today - timedelta(days=lookback - 1)
    by_day, last7 = defaultdict(float), defaultdict(float)
    for a in activities:
        if not (start <= a["day"] <= today):
            continue
        v = impact(a)
        by_day[a["day"]] += v
        if v and a["day"] > today - timedelta(days=7):
            src = "run" if a["type"] in RUN_TYPES else "legs" if a["type"] in WEIGHTS_TYPES else "walk"
            last7[src] += v
    daily = [by_day[start + timedelta(days=i)] for i in range(lookback)]
    acute, chronic = ewma(daily, ACUTE_DAYS) * 7, ewma(daily, CHRONIC_DAYS) * 7
    ratio = round(acute / chronic, 2) if chronic >= MIN_CHRONIC_WEEK else None
    cardio = round(atl / ctl, 2) if ctl and atl is not None and ctl >= 10 else None
    return {"impact_week": round(acute, 1), "impact_base_week": round(chronic, 1), "impact_ratio": ratio,
            "cardio_ratio": cardio, "last7": {k: round(v, 1) for k, v in last7.items()}}


def finding(s: dict) -> tuple[str, str, str] | None:
    """(severity, title, detail) for the injury report, or None without enough base. Only ever a
    caution: it limits running (core/readiness.running_limit), never the bike or the day."""
    r = s["impact_ratio"]
    if r is None:
        return None
    src = ", ".join(f"{k} {v:g}" for k, v in sorted(s["last7"].items(), key=lambda kv: -kv[1])) or "none"
    cardio = f"cardio ratio {s['cardio_ratio']:.2f}" if s["cardio_ratio"] is not None else "cardio ratio n/a"
    title = f"Impact load ratio {r:.2f} ({cardio})"
    base = (f"Running/leg impact {s['impact_week']:g} km-eq/week vs a base of {s['impact_base_week']:g} "
            f"(last 7 days: {src}).")
    if r >= IMPACT_CAUTION:
        calm = s["cardio_ratio"] is not None and s["cardio_ratio"] < IMPACT_CAUTION - 0.2
        return ("caution", title, base + (" Cardio load is calm, so the spike is in the legs, not the engine:"
                                          if calm else "") +
                " keep runs easy and put the extra work on the bike until the ratio settles.")
    return ("ok", title, base + " Bones and tendons are keeping up with the running.")
