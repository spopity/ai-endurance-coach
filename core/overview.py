"""
Athlete overview shared by the dashboard (/api/overview) and the Telegram
morning briefing, so both always show the same numbers.
"""
import logging
import statistics
import time
from datetime import date, datetime, timedelta

from core.db import get_connection
from core.injury_checks import run_injury_checks
from core.metrics import ftp_drift, load_athlete_profile, load_current_training_block
from core.race_planner import compute_race_plan
from core.races import fetch_upcoming_races

logger = logging.getLogger("AI_COACH_V1.overview")

_cache: dict = {}


def cached(key: str, ttl: int, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def query(sql: str, params=()) -> list[dict]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def rnd(v, digits=1):
    return round(float(v), digits) if v is not None else None


# --- overview ---

def hrv_status() -> dict | None:
    rows = query("""
        SELECT day, hrv FROM daily_metrics
        WHERE hrv IS NOT NULL AND day > CURRENT_DATE - 67 ORDER BY day;
    """)
    recent = [float(r["hrv"]) for r in rows if r["day"] > date.today() - timedelta(days=7)]
    base = [float(r["hrv"]) for r in rows if r["day"] <= date.today() - timedelta(days=7)]
    if len(recent) < 2 or len(base) < 10:
        return None
    mean, sd = statistics.fmean(base), statistics.stdev(base)
    avg7 = statistics.fmean(recent)
    status = "low" if avg7 < mean - sd else "below" if avg7 < mean - 0.5 * sd else "normal"
    return {"avg7": rnd(avg7), "baseline": rnd(mean), "sd": rnd(sd), "latest": rnd(rows[-1]["hrv"]), "status": status}


def next_race_summary() -> dict | None:
    try:
        races = cached("races", 600, fetch_upcoming_races)
    except Exception as e:
        logger.warning(f"Could not fetch races: {e}")
        return None
    if not races:
        return None
    race = races[0]
    summary = {k: race[k] for k in ("id", "name", "date", "days_until", "priority", "type", "distance_km",
                                     "elevation_m", "location", "goal", "missing")}
    if race.get("distance_km"):
        try:
            plan = cached(f"plan:{race['id']}:{race['date']}", 3600, lambda: compute_race_plan(race["id"]))
            summary["predicted_hours"] = round(plan["hours"], 2)
            if plan.get("pacing"):
                summary["target_np"] = round(plan["pacing"]["np"])
            if plan.get("taper"):
                summary["projected_tsb"] = round(plan["taper"]["maintain"]["tsb"], 1)
                summary["target_tsb"] = plan["taper"]["target"]
            if plan.get("weather"):
                summary["weather"] = plan["weather"].get("summary")
        except Exception as e:
            logger.warning(f"Race plan for dashboard failed: {e}")
    return summary


def build_overview() -> dict:
    """Everything the dashboard header/cards and the Telegram briefing show."""
    conn = get_connection()
    try:
        profile = load_athlete_profile(conn)
        block = load_current_training_block(conn)
    finally:
        conn.close()
    latest = query("SELECT day, ctl, atl, ramp_rate FROM daily_metrics WHERE ctl IS NOT NULL ORDER BY day DESC LIMIT 8;")
    last_sync = query("SELECT MAX(updated_at) AS t FROM activities;")[0]["t"]
    week_start = date.today() - timedelta(days=date.today().weekday())
    week = query("""
        SELECT COALESCE(SUM(training_load), 0) AS load, COALESCE(SUM(moving_time_sec), 0) AS secs, COUNT(*) AS n
        FROM activities WHERE superseded_by IS NULL AND start_time::date >= %s;
    """, (week_start,))[0]

    fitness = None
    if latest:
        now, week_ago = latest[0], latest[-1]
        ctl, atl = float(now["ctl"]), float(now["atl"])
        fitness = {
            "day": now["day"].isoformat(), "ctl": rnd(ctl), "atl": rnd(atl), "tsb": rnd(ctl - atl),
            "ramp_rate": rnd(now["ramp_rate"]),
            "ctl_change_7d": rnd(ctl - float(week_ago["ctl"])),
        }
    weight = float(profile.get("weight_kg") or 0) or None
    ftp = profile.get("ftp")

    try:
        injury = cached("injury", 300, run_injury_checks)
        injury_summary = {"overall": injury["overall"], "counts": injury["counts"],
                          "top": [f for f in injury["findings"] if f["severity"] in ("high", "caution")][:3]}
    except Exception as e:
        logger.warning(f"Injury checks failed: {e}")
        injury_summary = None

    return {
        "today": date.today().isoformat(),
        "athlete": {"ftp": ftp, "lthr": profile.get("fthr"), "weight_kg": weight,
                    "wkg": round(ftp / weight, 2) if ftp and weight else None,
                    "eftp": rnd(profile.get("eftp"), 0), "max_hr": profile.get("max_hr"),
                    "ftp_drift": ftp_drift(ftp, profile.get("eftp")),
                    "synced_at": profile.get("synced_at")},
        "fitness": fitness,
        "hrv": hrv_status(),
        "week": {"start": week_start.isoformat(), "load": rnd(week["load"], 0),
                 "hours": rnd(float(week["secs"]) / 3600), "activities": week["n"]},
        "block": {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in block.items()} if block else None,
        "injury": injury_summary,
        "next_race": next_race_summary(),
        "last_sync": last_sync.isoformat() if last_sync else None,
    }


# --- charts ---



def clear_cache():
    _cache.clear()
