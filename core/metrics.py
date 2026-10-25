"""
Aggregates athlete state from athlete_twin_v2 into the single context dict
the coaching agents will reason over. Successor to the old ai-coach's
metrics_utils.py, but reads Intervals.icu's own CTL/ATL/ramp_rate directly
from daily_metrics instead of re-deriving them from a raw TSS history.
"""
import json
import logging
import re
from datetime import datetime, date, timedelta

from core.db import get_connection
from core.focus import adjust_focus_text, get_focus
from core.gym import gym_focus
from core import availability
from core.feedback import context as feedback_context
from core.periodization import GOAL_NO_RACE, return_status, training_days

logger = logging.getLogger("AI_COACH_V1.metrics")

RECENT_ACTIVITY_COLUMNS = """
    id, external_id, source, start_time, type, name,
    distance_m, elapsed_time_sec, moving_time_sec, total_elevation_gain_m,
    avg_power_w, normalized_power_w, intensity_factor, training_load,
    efficiency_factor, decoupling_pct, variability_index,
    avg_hr, max_hr, avg_cadence, avg_lr_balance,
    feel, rpe, icu_ctl_at_time, icu_atl_at_time
"""


def _jsonify(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _clean_row(row: dict) -> dict:
    return {k: _jsonify(v) for k, v in row.items()}


def load_athlete_profile(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ftp, fthr, weight_kg, max_hr, resting_hr, eftp, w_prime_j, p_max_w, eftp_day,
                   source, synced_at, updated_at
            FROM athlete_profile WHERE id = 1;
        """)
        row = cur.fetchone()
        if not row:
            logger.warning("athlete_profile is empty - using hardcoded fallback.")
            return {"ftp": 250, "fthr": 165, "weight_kg": 75}
        profile = _clean_row(dict(row))
        profile.update(training_ftp(cur, profile.get("ftp"), profile.get("eftp")))
        return profile


# The FTP that targets are built on, following the test rhythm of every 6-8 weeks: the set
# FTP (Intervals.icu) is trusted while it is fresh; from FTP_TEST_DUE_DAYS a test is
# suggested, and once it hasn't been updated or retested for EFTP_FALLBACK_DAYS,
# Intervals.icu's eFTP (from recent maximal efforts) is used instead until the next test.
FTP_TEST_DUE_DAYS = 42          # 6 weeks: time for a test
EFTP_FALLBACK_DAYS = 56         # 8 weeks without one: train on eFTP
FTP_TEST_NAME = re.compile(r"\b(ftp|ramp)\b.*\btest\b|\btest\b.*\bftp\b|\b20 ?min(ute)?s? test\b", re.I)
CYCLING_TYPES = ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide")


def training_ftp(cur, ftp, eftp) -> dict:
    """{ftp (the one to train with), ftp_set, ftp_source, ftp_updated, ftp_age_days, ftp_test_due}.
    'Updated' is the later of: the first ride with the current set FTP (Intervals.icu stamps
    each activity with the FTP at the time) and the last ride named like an FTP test."""
    out = {"ftp_set": ftp, "ftp_source": "set", "ftp_updated": None, "ftp_age_days": None, "ftp_test_due": False}
    if not ftp:
        return {**out, "ftp": ftp}
    cur.execute("""
        SELECT start_time::date AS day, ftp_at_time, name FROM activities
        WHERE superseded_by IS NULL AND type = ANY(%s) AND ftp_at_time IS NOT NULL
        ORDER BY start_time DESC LIMIT 2000;
    """, (list(CYCLING_TYPES),))
    since = None
    rows = cur.fetchall()
    for r in rows:
        if round(float(r["ftp_at_time"])) != round(float(ftp)):
            break
        since = r["day"]
    tests = [r["day"] for r in rows if FTP_TEST_NAME.search(r["name"] or "")]
    updated = max([d for d in (since, tests[0] if tests else None) if d], default=None)
    age = (date.today() - updated).days if updated else None
    out.update(ftp_updated=updated.isoformat() if updated else None, ftp_age_days=age,
               ftp_test_due=age is None or age > FTP_TEST_DUE_DAYS)
    if (age is None or age > EFTP_FALLBACK_DAYS) and eftp:
        return {**out, "ftp": round(float(eftp)), "ftp_source": "eftp"}
    return {**out, "ftp": ftp}


# Set FTP vs Intervals.icu eFTP: eFTP sinking is often just a lack of recent hard
# efforts, so drift is flagged for the athlete to act on - never applied silently.
EFTP_BELOW_PCT = 5
EFTP_ABOVE_PCT = 3


def ftp_drift(ftp, eftp, profile: dict | None = None) -> dict | None:
    """Compares set FTP with eFTP. Returns {pct, status, message} or None without data.
    status: "below" (eFTP >5% lower), "above" (eFTP >3% higher) or "ok". With `profile`
    (load_athlete_profile), says when targets already use eFTP and when a test is due."""
    if not ftp or not eftp:
        return None
    pct = round((float(eftp) / float(ftp) - 1) * 100, 1)
    p = profile or {}
    when = f"last updated {p['ftp_age_days']} days ago" if p.get("ftp_age_days") is not None else "no update found"
    if p.get("ftp_source") == "eftp":
        return {"pct": pct, "status": "below" if pct < 0 else "above" if pct > 0 else "due", "message": (
            f"No FTP test for over {EFTP_FALLBACK_DAYS // 7} weeks ({when}), so targets use eFTP {eftp:.0f} W "
            f"instead of your set FTP {ftp:.0f} W. Do an FTP test (ramp or 20 min) and update FTP in Intervals.icu - "
            "targets go back to the set FTP from then.")}
    if pct <= -EFTP_BELOW_PCT and p.get("ftp_age_days") is not None and p["ftp_age_days"] <= EFTP_FALLBACK_DAYS:
        status = "below"
        message = (f"eFTP {eftp:.0f} W is {abs(pct):.0f}% below your set FTP {ftp:.0f} W, but FTP was {when} - "
                   "eFTP is probably just missing recent maximal efforts. Targets stay on the set FTP; after "
                   f"{EFTP_FALLBACK_DAYS // 7} weeks without a test they switch to eFTP.")
    elif pct <= -EFTP_BELOW_PCT:
        status = "below"
        message = (f"eFTP {eftp:.0f} W is {abs(pct):.0f}% below your set FTP {ftp:.0f} W - power targets may be "
                   "too high. Do an FTP test or a hard 20-min effort; if it confirms, lower FTP in Intervals.icu.")
    elif pct >= EFTP_ABOVE_PCT:
        status = "above"
        message = (f"eFTP {eftp:.0f} W is {pct:.0f}% above your set FTP {ftp:.0f} W - your FTP looks outdated. "
                   "Raise it in Intervals.icu so zones and targets keep up.")
    else:
        status = "ok"
        message = f"eFTP {eftp:.0f} W agrees with your set FTP {ftp:.0f} W."
    if p.get("ftp_test_due"):
        message += (f" FTP {when} - a test is due (every 6-8 weeks); after {EFTP_FALLBACK_DAYS // 7} weeks without "
                    "one, targets switch to eFTP.")
        status = "due" if status == "ok" else status
    return {"pct": pct, "status": status, "message": message}


def load_latest_wellness(conn) -> dict:
    """Most recent daily_metrics row, whatever day that happens to be."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT day, ctl, atl, ramp_rate, sleep_score, sleep_duration_sec,
                   resting_hr, hrv, weight_kg, steps
            FROM daily_metrics
            ORDER BY day DESC
            LIMIT 1;
        """)
        row = cur.fetchone()
        if not row:
            logger.warning("daily_metrics is empty - no wellness/PMC data available yet.")
            return {}
        wellness = _clean_row(dict(row))
        if wellness.get("sleep_duration_sec") is not None:
            wellness["sleep_duration_hours"] = round(wellness["sleep_duration_sec"] / 3600, 2)
        ctl, atl = wellness.get("ctl"), wellness.get("atl")
        wellness["tsb"] = round(ctl - atl, 1) if ctl is not None and atl is not None else None
        return wellness


def load_recent_activities(conn, limit: int = 5) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT {RECENT_ACTIVITY_COLUMNS}
            FROM activities
            WHERE superseded_by IS NULL
            ORDER BY start_time DESC
            LIMIT %s;
        """, (limit,))
        rows = [_clean_row(dict(r)) for r in cur.fetchall()]
        for r in rows:
            if r.get("distance_m"):
                r["distance_km"] = round(r["distance_m"] / 1000, 2)
            if r.get("moving_time_sec"):
                r["duration_hours"] = round(r["moving_time_sec"] / 3600, 2)
            if gym := gym_focus(r.get("type"), r.get("name")):
                r["gym_focus"] = gym
        return rows


def search_historical_activities(sport_type: str, min_distance_meters: float = 0) -> str:
    """Searches the athlete's entire history for specific lifetime achievements.
    Kept as a plain function (not a method) so it can be wrapped as an agent
    tool the same way the old system did.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT start_time, type, distance_m, moving_time_sec
                FROM activities
                WHERE type ILIKE %s AND distance_m >= %s AND superseded_by IS NULL
                ORDER BY start_time DESC;
            """, (f"%{sport_type}%", min_distance_meters))
            rows = cur.fetchall()
            if not rows:
                return "No activities found matching that criteria."
            formatted = "Found historical activities:\n"
            for row in rows[:3]:
                date_str = row["start_time"].strftime("%Y-%m-%d")
                dist_km = round(row["distance_m"] / 1000, 2) if row["distance_m"] else 0
                formatted += f"- {date_str}: {row['type']}, {dist_km} km\n"
            return formatted
    except Exception as e:
        logger.error(f"Database error in search_historical_activities: {e}", exc_info=True)
        return f"Database error: {str(e)}"
    finally:
        conn.close()


# ~2 years of daily rows: enough for a year-over-year seasonal comparison while keeping the
# tool result (one JSON object per day) a reasonable size for the LLM context.
HISTORICAL_WELLNESS_MAX_DAYS = 731


def get_historical_wellness_data(start_date: str, end_date: str) -> str:
    """Daily wellness rows (HRV, resting HR, sleep, CTL/ATL/TSB) from daily_metrics for an
    inclusive date range, as a JSON array ordered by date - so agents can look at seasonal
    trends or past baselines beyond the single latest day in metrics_context.
    """
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except (TypeError, ValueError):
        return f"Invalid date: start_date='{start_date}', end_date='{end_date}'. Use YYYY-MM-DD."
    if start > end:
        return f"start_date ({start_date}) is after end_date ({end_date})."
    if (end - start).days + 1 > HISTORICAL_WELLNESS_MAX_DAYS:
        return (f"Date range too long ({(end - start).days + 1} days). Query at most "
                f"{HISTORICAL_WELLNESS_MAX_DAYS} days at a time, e.g. one call per season or year.")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT day AS date, hrv, resting_hr, sleep_score,
                       ROUND(sleep_duration_sec / 3600.0, 2)::float AS sleep_duration_hours,
                       ctl, atl,
                       ROUND((ctl - atl)::numeric, 1)::float AS tsb
                FROM daily_metrics
                WHERE day BETWEEN %s AND %s
                ORDER BY day;
            """, (start, end))
            rows = [_clean_row(dict(r)) for r in cur.fetchall()]
        if not rows:
            return f"No wellness data found between {start_date} and {end_date}."
        return json.dumps(rows)
    except Exception as e:
        logger.error(f"Database error in get_historical_wellness_data: {e}", exc_info=True)
        return f"Database error: {str(e)}"
    finally:
        conn.close()


def _format_duration(total_seconds) -> str:
    """Formats a duration in seconds: 'Ns' under a minute, 'Mm Ss' once it
    reaches a minute - e.g. 45 -> '45s', 85 -> '1m 25s', 1542 -> '25m 42s'."""
    total_seconds = int(round(total_seconds))
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}m {seconds}s"


def _reformat_interval_summary(intervals: list) -> list:
    """Intervals.icu gives these as pre-formatted strings like '8x 85s 316w' -
    rewrite just the seconds portion through _format_duration for consistency
    with the rest of this function's output."""
    return [re.sub(r"(\d+)s", lambda m: _format_duration(int(m.group(1))), s) for s in intervals]


def get_activity_details(external_id: str) -> str:
    """Fetches the full interval/zone/power-curve breakdown for one specific
    activity by its external_id (as seen in the activities list already in
    metrics_context). The flat Tier-1 columns used for the default recent-
    activities list don't include this level of detail - it's stored in
    raw_metrics and only worth fetching when a query actually asks for it,
    to keep the default context lean.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT name, type, start_time, raw_metrics
                FROM activities WHERE external_id = %s;
            """, (external_id,))
            row = cur.fetchone()
            if not row:
                return f"No activity found with external_id='{external_id}'."

            raw = row.get("raw_metrics") or {}
            header = f"Detailed breakdown for '{row['name']}' ({row['type']}, {row['start_time'].strftime('%Y-%m-%d')}):"
            lines = []

            if raw.get("interval_summary"):
                lines.append("Intervals: " + "; ".join(_reformat_interval_summary(raw["interval_summary"])))
            if raw.get("icu_zone_times"):
                zones = ", ".join(f"{z['id']}={_format_duration(z['secs'])}" for z in raw["icu_zone_times"])
                lines.append(f"Power zone time: {zones}")
            if raw.get("icu_power_zones"):
                lines.append(f"Power zone boundaries (% of FTP): {raw['icu_power_zones']}")
            if raw.get("icu_hr_zone_times"):
                hr_zones = ", ".join(f"Z{i+1}={_format_duration(s)}" for i, s in enumerate(raw["icu_hr_zone_times"]))
                lines.append(f"HR zone time: {hr_zones}")
            if raw.get("icu_hr_zones"):
                lines.append(f"HR zone boundaries (bpm): {raw['icu_hr_zones']}")
            if raw.get("icu_sweet_spot_min") is not None and raw.get("icu_sweet_spot_max") is not None:
                lines.append(f"Sweet spot range: {raw['icu_sweet_spot_min']}-{raw['icu_sweet_spot_max']}% FTP")
            if raw.get("icu_warmup_time") is not None:
                cooldown = raw.get("icu_cooldown_time")
                cooldown_str = _format_duration(cooldown) if cooldown is not None else "n/a"
                lines.append(f"Warmup: {_format_duration(raw['icu_warmup_time'])}, Cooldown: {cooldown_str}")
            if raw.get("icu_hrr"):
                hrr = raw["icu_hrr"]
                lines.append(f"Heart rate recovery: {hrr.get('start_bpm')} -> {hrr.get('end_bpm')} bpm ({hrr.get('hrr')} bpm drop)")

            if not lines:
                return header + " No detailed interval/zone data is available for this activity."
            return header + "\n" + "\n".join(lines)
    except Exception as e:
        logger.error(f"Database error in get_activity_details: {e}", exc_info=True)
        return f"Database error: {str(e)}"
    finally:
        conn.close()


def load_current_training_block(conn) -> dict:
    """Returns today's row from the rolling periodization plan (training_blocks),
    plus how much training load has actually been logged so far within that
    block's date range - so ai_coach can compare actual-so-far against the
    planned target, not just describe the plan in the abstract.
    """
    today = date.today()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT phase, start_date, end_date, target_weekly_load_min,
                   target_weekly_load_max, primary_focus, is_deload, notes AS why
            FROM training_blocks
            WHERE start_date <= %s AND end_date >= %s
            ORDER BY start_date DESC LIMIT 1;
        """, (today, today))
        row = cur.fetchone()
        if not row:
            return {}

        cur.execute("""
            SELECT COALESCE(SUM(training_load), 0) AS load_so_far
            FROM activities
            WHERE start_time::date >= %s AND start_time::date <= %s AND superseded_by IS NULL;
        """, (row["start_date"], today))
        load_so_far = cur.fetchone()["load_so_far"]

    block = _clean_row(dict(row))
    mode = get_focus()["mode"]
    block["primary_focus"] = adjust_focus_text(block["primary_focus"], block["is_deload"], mode, block["phase"])
    block["load_so_far_this_block"] = round(float(load_so_far), 1)
    block["days_left_this_week"] = (row["end_date"] - today).days + 1  # including today

    # Planned vs done sessions, so the coach can pick the key ones when the week runs short.
    from core.plan import week_block, week_done  # lazy: core.plan -> core.overview -> core.metrics
    planned = (week_block(row["start_date"]) or {}).get("sessions") or {}
    done, _ = week_done(row["start_date"])
    block["sessions_planned"] = {k: int(v) for k, v in planned.items() if v}
    block["sessions_done"] = {k: v for k, v in done.items() if v}
    block["sessions_remaining"] = {k: int(v) - done.get(k, 0) for k, v in planned.items() if int(v) > done.get(k, 0)}
    return block


def _safe_context(name: str, fn):
    try:
        return fn()
    except Exception as e:
        logger.warning(f"Context '{name}' unavailable: {e}")
        return None


def _plan_goal() -> str:
    """What the plan works towards: the next A (or B) race, else fitness + FTP."""
    from core.overview import cached  # lazy: core.overview imports this module
    from core.races import fetch_upcoming_races
    races = _safe_context("races", lambda: cached("races", 600, fetch_upcoming_races)) or []
    for prio in ("A", "B"):
        r = next((r for r in races if r.get("priority") == prio), None)
        if r:
            return (f"{prio} race {r['name']} on {r['date']} (in {r['days_until']} days) - the plan builds towards it "
                    "(race prep, taper, race week).")
    return GOAL_NO_RACE


def _intensity_distribution():
    from core.injury_checks import _load_data, intensity_distribution
    conn = get_connection()
    try:
        return intensity_distribution(_load_data(conn, date.today())["activities"], date.today())
    finally:
        conn.close()


def load_athlete_metrics() -> dict:
    """The single context dict fed to every coaching agent."""
    conn = get_connection()
    try:
        profile = load_athlete_profile(conn)
        wellness = load_latest_wellness(conn)
        recent_activities = load_recent_activities(conn, limit=5)
        current_block = load_current_training_block(conn)
        today = date.today()
        avail = _safe_context("availability", availability.context)
        sick = next((e for e in (avail or {}).get("entries", [])
                     if e["kind"] == "sick" and today.isoformat() in e["dates"]), None)
        sick_until = date.fromisoformat(sick["until"]) if sick else None
        illness_end = date.fromisoformat(avail["illness_ended"]) if avail and avail.get("illness_ended") else None
        back = return_status(training_days(conn, today - timedelta(days=60)), today, sick_until, illness_end)
        if current_block:
            current_block["goal"] = _plan_goal()

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM activities WHERE superseded_by IS NULL;")
            total_activities = cur.fetchone()["n"]
            cur.execute("""
                SELECT MAX(moving_time_sec) AS m FROM activities WHERE superseded_by IS NULL;
            """)
            longest_sec = cur.fetchone()["m"]

        return {
            "current_date": date.today().isoformat(),
            "ftp": profile.get("ftp"),
            "eftp": profile.get("eftp"),
            "ftp_set": profile.get("ftp_set"),
            "ftp_source": profile.get("ftp_source"),  # "eftp" when there was no FTP test for over 8 weeks
            "ftp_updated": profile.get("ftp_updated"),
            "ftp_check": (ftp_drift(profile.get("ftp_set"), profile.get("eftp"), profile) or {}).get("message"),
            "max_hr": profile.get("max_hr"),
            "resting_hr_setting": profile.get("resting_hr"),
            "fthr": profile.get("fthr"),
            "athlete_weight": wellness.get("weight_kg") or profile.get("weight_kg"),
            "ctl": wellness.get("ctl"),
            "atl": wellness.get("atl"),
            "tsb": wellness.get("tsb"),
            "ramp_rate": wellness.get("ramp_rate"),
            "wellness": wellness,
            "training_focus": get_focus()["mode"],
            "current_training_block": current_block,
            "return_to_training": back,
            "availability": avail,
            "athlete_feedback": _safe_context("feedback", feedback_context),
            "intensity_distribution": _safe_context("intensity distribution", _intensity_distribution),
            "activities": recent_activities,
            "total_activities": total_activities or 0,
            "longest_activity_duration_hours": round(longest_sec / 3600, 2) if longest_sec else 0,
        }
    except Exception as e:
        logger.error(f"Error while loading athlete metrics: {e}", exc_info=True)
        return {"error": str(e)}
    finally:
        conn.close()
