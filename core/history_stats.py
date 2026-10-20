"""
Deterministic calculators over the athlete's history for the history_analyst agent - the
same split as the current-status path (metrics/readiness/injury checks): every number is
computed here, locally, and Gemini only chooses which calculation to run and interprets
the result. It never averages, differences or classifies raw rows itself.

- wellness_summary: HRV / resting HR / sleep / CTL / ATL / TSB / weight ... over a period,
  with weekly/monthly series, trend and an optional comparison period.
- training_summary: volume, load and workout-type mix per week/month/year, with CTL change.
- fitness_gain_windows: the stretches where CTL rose most, and the training done in them.
- race_history: past races (flag or name), newest first, with power pacing per race.
"""
import logging
import re
import statistics
from datetime import date, timedelta

from core.db import get_connection
from core.pacing import analyze_power_pacing

logger = logging.getLogger("AI_COACH_V1.history_stats")

LOCAL_DATE = "(start_time AT TIME ZONE 'Europe/Bratislava')::date"

# name -> (SQL expression over daily_metrics, unit). Fixed expressions only, never user input.
WELLNESS_METRICS = {
    "hrv": ("hrv", "ms"),
    "resting_hr": ("resting_hr", "bpm"),
    "sleep_score": ("sleep_score", "0-100"),
    "sleep_hours": ("sleep_duration_sec / 3600.0", "h"),
    "avg_sleeping_hr": ("avg_sleeping_hr", "bpm"),
    "ctl": ("ctl", "fitness (CTL)"),
    "atl": ("atl", "fatigue (ATL)"),
    "tsb": ("ctl - atl", "form (TSB)"),
    "ramp_rate": ("ramp_rate", "CTL/week"),
    "weight_kg": ("weight_kg", "kg"),
    "steps": ("steps", "steps"),
}

SPORT_FILTERS = {
    "all": "TRUE",
    "cycling": "type ILIKE '%%Ride%%' AND type <> 'EMountainBikeRide'",
    "running": "type IN ('Run', 'TrailRun', 'VirtualRun')",
}
GROUP_BYS = ("week", "month", "year", "none")
MAX_SERIES_POINTS = 60

RACE_NAME_RE = r"(\mrace\M|závod|zavod|preteky|maraton|marathon|gran ?fondo|etape|\mcup\M|pohár)"
# Warm-up / cool-down / recon rides recorded around a race ("Pre-race", "Post-race") aren't races.
NOT_RACE_NAME_RE = r"(pre|post)[- ]?race|warm.?up|cool.?down|\mrecon"
RACE_MIN_SEC = 20 * 60
NON_RACE_TYPES = ("Walk", "Hike", "WeightTraining", "Yoga", "Workout")


def _period(start_date: str, end_date: str) -> tuple[date, date]:
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end:
        raise ValueError(f"start_date ({start_date}) is after end_date ({end_date})")
    return start, end


def _bucket(day: date, group_by: str) -> str:
    if group_by == "week":
        return (day - timedelta(days=day.weekday())).isoformat()
    if group_by == "month":
        return day.strftime("%Y-%m")
    if group_by == "year":
        return str(day.year)
    return "all"


def _coarsen(group_by: str, start: date, end: date) -> str:
    """Steps week -> month -> year until the series fits MAX_SERIES_POINTS."""
    days = (end - start).days + 1
    if group_by == "week" and days / 7 > MAX_SERIES_POINTS:
        group_by = "month"
    if group_by == "month" and days / 30.4 > MAX_SERIES_POINTS:
        group_by = "year"
    return group_by


def _r(x, nd=1):
    return round(x, nd) if x is not None else None


def _slope_per_week(points: list[tuple[date, float]]) -> float | None:
    """Least-squares slope of value over time, in units per week."""
    if len(points) < 7:
        return None
    xs = [p[0].toordinal() for p in points]
    ys = [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den * 7 if den else None


def _stats(points: list[tuple[date, float]], start: date, end: date) -> dict:
    days_in_period = (end - start).days + 1
    if not points:
        return {"days_with_data": 0, "days_in_period": days_in_period, "note": "no data in this period"}
    values = [v for _, v in points]
    lo, hi = min(points, key=lambda p: p[1]), max(points, key=lambda p: p[1])
    first = [v for d, v in points if d < start + timedelta(days=7)]
    last = [v for d, v in points if d > end - timedelta(days=7)]
    out = {
        "days_with_data": len(points),
        "days_in_period": days_in_period,
        "first_day_with_data": points[0][0].isoformat(),
        "last_day_with_data": points[-1][0].isoformat(),
        "mean": _r(statistics.fmean(values)),
        "median": _r(statistics.median(values)),
        "sd": _r(statistics.stdev(values)) if len(values) > 1 else None,
        "min": {"value": _r(lo[1]), "date": lo[0].isoformat()},
        "max": {"value": _r(hi[1]), "date": hi[0].isoformat()},
        "first_7_days_mean": _r(statistics.fmean(first)) if first else None,
        "last_7_days_mean": _r(statistics.fmean(last)) if last else None,
        "trend_per_week": _r(_slope_per_week(points), 2),
    }
    if first and last:
        out["change_first_to_last_7_days"] = _r(out["last_7_days_mean"] - out["first_7_days_mean"])
    if len(points) < days_in_period * 0.5:
        out["note"] = f"data on only {len(points)} of {days_in_period} days - treat with caution"
    return out


def _series(points: list[tuple[date, float]], group_by: str) -> list[dict]:
    buckets: dict[str, list[float]] = {}
    for d, v in points:
        buckets.setdefault(_bucket(d, group_by), []).append(v)
    return [{"period": k, "mean": _r(statistics.fmean(vs)), "min": _r(min(vs)), "max": _r(max(vs)), "days": len(vs)}
            for k, vs in buckets.items()]


def _wellness_points(cur, metrics: list[str], start: date, end: date) -> dict[str, list[tuple[date, float]]]:
    cols = ", ".join(f"{WELLNESS_METRICS[m][0]} AS {m}" for m in metrics)
    cur.execute(f"SELECT day, {cols} FROM daily_metrics WHERE day BETWEEN %s AND %s ORDER BY day;", (start, end))
    rows = cur.fetchall()
    return {m: [(r["day"], float(r[m])) for r in rows if r[m] is not None] for m in metrics}


def wellness_summary(metrics: list[str], start_date: str, end_date: str, group_by: str = "week",
                     compare_start_date: str | None = None, compare_end_date: str | None = None) -> dict:
    unknown = [m for m in metrics if m not in WELLNESS_METRICS]
    if unknown or not metrics:
        return {"error": f"Unknown metrics {unknown}. Choose from: {sorted(WELLNESS_METRICS)}"}
    if group_by not in GROUP_BYS:
        return {"error": f"group_by must be one of {GROUP_BYS}"}
    start, end = _period(start_date, end_date)
    group_by = _coarsen(group_by, start, end)
    compare = _period(compare_start_date, compare_end_date) if compare_start_date and compare_end_date else None

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            points = _wellness_points(cur, metrics, start, end)
            compare_points = _wellness_points(cur, metrics, *compare) if compare else None
    finally:
        conn.close()

    result = {"period": {"start": start.isoformat(), "end": end.isoformat()}, "group_by": group_by, "metrics": {}}
    for m in metrics:
        entry = {"unit": WELLNESS_METRICS[m][1], **_stats(points[m], start, end)}
        if points[m] and group_by != "none":
            entry["series"] = _series(points[m], group_by)
        if compare:
            c = _stats(compare_points[m], *compare)
            entry["comparison"] = {"period": {"start": compare[0].isoformat(), "end": compare[1].isoformat()},
                                   **{k: c.get(k) for k in ("days_with_data", "mean", "median", "min", "max",
                                                            "trend_per_week", "note")}}
            if entry.get("mean") is not None and c.get("mean") is not None:
                entry["comparison"]["mean_difference_this_minus_comparison"] = _r(entry["mean"] - c["mean"])
                if c["mean"]:
                    entry["comparison"]["mean_difference_pct"] = _r((entry["mean"] / c["mean"] - 1) * 100)
        result["metrics"][m] = entry
    return result


def classify_workout(a: dict) -> str:
    """Workout category from IF, variability index and HR - the same bands the coach uses.
    'steady' = variability index <= 1.05 (continuous effort); without power, HR vs LTHR decides."""
    t = a["type"] or ""
    if t == "WeightTraining":
        return "strength"
    if not ("Ride" in t or "Run" in t or "Swim" in t or t in ("VirtualRow", "NordicSki")):
        return "other"
    ifactor, vi = a.get("intensity_factor"), a.get("variability_index")
    if ifactor and a.get("avg_power_w"):
        if ifactor >= 0.95:
            return "race / threshold+"
        steady = vi is not None and vi <= 1.05
        if ifactor < 0.75:
            return "steady endurance (Z2)" if steady else "variable endurance"
        if ifactor < 0.85:
            return "steady tempo" if steady else "variable tempo / mixed"
        return "steady sweet spot / threshold" if steady else "intervals / hard variable"
    if a.get("avg_hr") and a.get("lthr"):
        ratio = a["avg_hr"] / a["lthr"]
        return "easy (HR)" if ratio < 0.85 else "moderate (HR)" if ratio < 0.92 else "hard (HR)"
    return "unclassified (no power/HR)"


def _new_agg() -> dict:
    return {"activities": 0, "hours": 0.0, "distance_km": 0.0, "tss": 0.0, "by_sport_hours": {}, "by_category": {}}


def _add(agg: dict, a: dict, category: str):
    hours = (a["moving_time_sec"] or 0) / 3600
    tss = a["training_load"] or 0
    agg["activities"] += 1
    agg["hours"] += hours
    agg["distance_km"] += (a["distance_m"] or 0) / 1000
    agg["tss"] += tss
    agg["by_sport_hours"][a["type"]] = agg["by_sport_hours"].get(a["type"], 0) + hours
    cat = agg["by_category"].setdefault(category, {"count": 0, "hours": 0.0, "tss": 0.0})
    cat["count"] += 1
    cat["hours"] += hours
    cat["tss"] += tss


def _finish(agg: dict) -> dict:
    total_h = agg["hours"] or 1
    return {
        "activities": agg["activities"],
        "hours": _r(agg["hours"]),
        "distance_km": _r(agg["distance_km"], 0),
        "tss": _r(agg["tss"], 0),
        "by_sport_hours": {k: _r(v) for k, v in sorted(agg["by_sport_hours"].items(), key=lambda kv: -kv[1])},
        "by_category": {k: {"count": v["count"], "hours": _r(v["hours"]), "tss": _r(v["tss"], 0),
                            "share_of_hours_pct": _r(v["hours"] / total_h * 100, 0)}
                        for k, v in sorted(agg["by_category"].items(), key=lambda kv: -kv[1]["hours"])},
    }


def _ctl_on(cur, days: list[date]) -> dict[date, float]:
    cur.execute("SELECT day, ctl FROM daily_metrics WHERE day = ANY(%s) AND ctl IS NOT NULL;", (days,))
    return {r["day"]: r["ctl"] for r in cur.fetchall()}


def _training(cur, start: date, end: date, group_by: str, sport: str) -> dict:
    cur.execute(f"""
        SELECT {LOCAL_DATE} AS day, type, moving_time_sec, distance_m, training_load, intensity_factor,
               variability_index, avg_power_w, avg_hr, lthr
        FROM activities
        WHERE superseded_by IS NULL AND {LOCAL_DATE} BETWEEN %s AND %s AND {SPORT_FILTERS[sport]}
        ORDER BY start_time;
    """, (start, end))
    total, buckets, bounds = _new_agg(), {}, {}
    for a in cur.fetchall():
        category = classify_workout(a)
        _add(total, a, category)
        if group_by != "none":
            key = _bucket(a["day"], group_by)
            _add(buckets.setdefault(key, _new_agg()), a, category)
            lo, hi = bounds.get(key, (a["day"], a["day"]))
            bounds[key] = (min(lo, a["day"]), max(hi, a["day"]))

    ctl = _ctl_on(cur, [start - timedelta(days=1), end])
    result = {"totals": _finish(total),
              "ctl_start": _r(ctl.get(start - timedelta(days=1))), "ctl_end": _r(ctl.get(end))}
    if result["ctl_start"] is not None and result["ctl_end"] is not None:
        result["ctl_change"] = _r(result["ctl_end"] - result["ctl_start"])
    if group_by != "none":
        result["series"] = [{"period": k, **_finish(v)} for k, v in buckets.items()]
    return result


def training_summary(start_date: str, end_date: str, group_by: str = "month", sport: str = "all") -> dict:
    if group_by not in GROUP_BYS:
        return {"error": f"group_by must be one of {GROUP_BYS}"}
    if sport not in SPORT_FILTERS:
        return {"error": f"sport must be one of {sorted(SPORT_FILTERS)}"}
    start, end = _period(start_date, end_date)
    group_by = _coarsen(group_by, start, end)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            result = _training(cur, start, end, group_by, sport)
    finally:
        conn.close()
    return {"period": {"start": start.isoformat(), "end": end.isoformat()}, "sport": sport, "group_by": group_by,
            **result}


def fitness_gain_windows(start_date: str, end_date: str, window_days: int = 28, top: int = 3,
                         sport: str = "all") -> dict:
    """The `top` non-overlapping windows of `window_days` with the largest CTL rise, each with
    the training done in it - for 'what built my fitness' questions."""
    if sport not in SPORT_FILTERS:
        return {"error": f"sport must be one of {sorted(SPORT_FILTERS)}"}
    start, end = _period(start_date, end_date)
    window_days, top = max(7, min(int(window_days), 120)), max(1, min(int(top), 5))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT day, ctl FROM daily_metrics WHERE day BETWEEN %s AND %s AND ctl IS NOT NULL ORDER BY day;",
                        (start, end))
            ctl = {r["day"]: r["ctl"] for r in cur.fetchall()}
            if len(ctl) < window_days:
                return {"error": "not enough CTL data in this period"}
            gains = sorted(((ctl[d + timedelta(days=window_days)] - v, d) for d, v in ctl.items()
                            if d + timedelta(days=window_days) in ctl), reverse=True)
            picked = []
            for gain, d in gains:
                if gain <= 0 or len(picked) == top:
                    break
                if all(abs((d - p).days) >= window_days for _, p in picked):
                    picked.append((gain, d))
            windows = []
            for gain, d in sorted(picked, key=lambda p: p[1]):
                w_end = d + timedelta(days=window_days)
                training = _training(cur, d + timedelta(days=1), w_end, "none", sport)
                windows.append({"start": (d + timedelta(days=1)).isoformat(), "end": w_end.isoformat(),
                                "ctl_from": _r(ctl[d]), "ctl_to": _r(ctl[w_end]), "ctl_gain": _r(gain),
                                "training": training["totals"]})
            overall = _training(cur, start, end, "none", sport)
    finally:
        conn.close()
    peak_day = max(ctl, key=ctl.get)
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "ctl_period_start": _r(ctl[min(ctl)]), "ctl_period_end": _r(ctl[max(ctl)]),
        "ctl_peak": {"value": _r(ctl[peak_day]), "date": peak_day.isoformat()},
        "window_days": window_days,
        "biggest_gain_windows": windows or "CTL never rose over a window of this length in the period",
        "whole_period_training": overall["totals"],
    }


def race_history(before_date: str | None = None, after_date: str | None = None, limit: int = 5,
                 include_pacing: bool = True) -> dict:
    """Past races newest first - flagged as race in Intervals.icu, or named like one - with
    summary numbers and, if asked, power pacing per race (core/pacing.py)."""
    limit = max(1, min(int(limit), 10))
    before = date.fromisoformat(before_date) if before_date else date.today() + timedelta(days=1)
    after = date.fromisoformat(after_date) if after_date else date(1900, 1, 1)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT external_id, {LOCAL_DATE} AS day, name, type, race, moving_time_sec, distance_m,
                       total_elevation_gain_m, avg_power_w, normalized_power_w, intensity_factor, training_load,
                       avg_hr, decoupling_pct
                FROM activities
                WHERE superseded_by IS NULL AND {LOCAL_DATE} < %s AND {LOCAL_DATE} > %s
                  AND type <> ALL(%s) AND (race OR (name ~* %s AND name !~* %s AND moving_time_sec >= %s))
                ORDER BY start_time DESC
                LIMIT %s;
            """, (before, after, list(NON_RACE_TYPES), RACE_NAME_RE, NOT_RACE_NAME_RE, RACE_MIN_SEC, limit + 1))
            rows = cur.fetchall()
    finally:
        conn.close()

    more = len(rows) > limit
    races = []
    for r in rows[:limit]:
        races.append({
            "external_id": r["external_id"], "date": r["day"].isoformat(), "name": r["name"], "type": r["type"],
            "detected_by": "race flag" if r["race"] else "name",
            "duration_h": _r((r["moving_time_sec"] or 0) / 3600, 2), "distance_km": _r((r["distance_m"] or 0) / 1000),
            "elevation_m": _r(r["total_elevation_gain_m"], 0), "np_w": _r(r["normalized_power_w"], 0),
            "avg_power_w": _r(r["avg_power_w"], 0), "intensity_factor": _r(r["intensity_factor"], 2),
            "tss": _r(r["training_load"], 0), "avg_hr": r["avg_hr"], "hr_decoupling_pct": _r(r["decoupling_pct"]),
        })
    if include_pacing and races:
        pacing = {p["external_id"]: p for p in analyze_power_pacing([r["external_id"] for r in races])}
        for r in races:
            p = pacing.get(r["external_id"], {})
            r["pacing"] = {k: v for k, v in p.items() if k not in ("external_id", "date", "name", "type")}
    return {
        "races": races,
        "detection": "race flag in Intervals.icu, or a name matching race/závod/preteky/maraton/marathon/"
                     "gran fondo/etape/cup/pohár (walks, hikes, gym, pre/post-race and warm-up rides, and anything "
                     "under 20 min excluded)",
        "older_races_available": more,
        "next_page": f"call again with before_date={races[-1]['date']}" if more and races else None,
    }
