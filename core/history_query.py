"""
Free-form, read-only SQL over the athlete's history for the history_analyst agent, so chat
questions about any past period ("HRV last summer", "which steady rides lifted my CTL in
2024", "last race where my power held") don't each need a hand-written tool.

Safety comes from the database, not from parsing SQL: queries run as the `coach_reader`
role (db/migrations/014_readonly_role.sql), which can only SELECT four data tables, inside a
READ ONLY transaction with a statement timeout. The SELECT/WITH check below only gives
the model a clear error early.
"""
import json
import logging
import re

import psycopg2
import psycopg2.extras

from core.db import load_env

logger = logging.getLogger("AI_COACH_V1.history_query")

MAX_ROWS = 200
MAX_CHARS = 40_000
STATEMENT_TIMEOUT_MS = 15_000

_STARTS_LIKE_SELECT = re.compile(r"^\s*(select|with)\b", re.I)

SCHEMA_GUIDE = """PostgreSQL 16. Tables you can read (all times UTC; the athlete lives in Europe/Bratislava -
use (start_time AT TIME ZONE 'Europe/Bratislava')::date for the local day of an activity):

daily_metrics - one row per day (`day` DATE, primary key). Garmin health data + Intervals.icu fitness model.
  Intervals.icu: ctl (fitness, 42-day load avg), atl (fatigue, 7-day), ramp_rate (CTL change/week).
  TSB (form) = ctl - atl (not stored). vo2max. weight_kg (Withings), body_fat_pct, kcal_consumed.
  Garmin (sleep fields describe the night that ENDS on `day`):
  sleep_score (0-100), sleep_quality (1 excellent - 4 poor), sleep_duration_sec, sleep_start, sleep_end (TIMESTAMPTZ),
  sleep_deep_sec, sleep_light_sec, sleep_rem_sec, sleep_awake_sec, sleep_unmeasurable_sec, nap_sec, sleep_awake_count,
  sleep_stress (0-100), sleep_respiration (+ _low, _high, breaths/min), sleep_feedback, avg_sleeping_hr (bpm).
  hrv (overnight rMSSD avg, ms), hrv_weekly_avg, hrv_5min_high, hrv_status (BALANCED/UNBALANCED/LOW/POOR),
  hrv_baseline_low / hrv_baseline_high (Garmin's balanced band), hrv_low_upper.
  resting_hr, resting_hr_7d_avg, min_hr, max_hr (bpm). stress_avg, stress_max (0-100 all day),
  stress_rest_sec, stress_low_sec, stress_medium_sec, stress_high_sec, stress_qualifier.
  body_battery_wake, body_battery_high, body_battery_low, body_battery_charged, body_battery_drained (0-100).
  respiration (awake avg) + respiration_low/_high. spo2 (daily avg %), spo2_lowest, spo2_sleep_avg.
  steps, total_kcal, active_kcal, intensity_min_moderate, intensity_min_vigorous, floors_ascended,
  wellness_distance_m, fitness_age.
  subjective 1-4/5 scales (mostly empty): soreness, fatigue, stress, mood, motivation, readiness, injury.
  comments TEXT. raw_payload / garmin_payload JSONB (full source records - avoid selecting them, they are large).

activities - one row per workout. ALWAYS filter `superseded_by IS NULL` (superseded rows are duplicates).
  external_id (e.g. 'i123456789' - pass to get_activity_details / analyze_power_pacing), name, type
  (Ride, VirtualRide, MountainBikeRide, GravelRide, EMountainBikeRide, Run, TrailRun, Walk, Hike, WeightTraining,
  Workout, Swim, OpenWaterSwim, Yoga, ...; "cycling" = type ILIKE '%Ride%' excluding EMountainBikeRide), sub_type,
  start_time TIMESTAMPTZ, race BOOLEAN (only set on a few races - also match names like '%race%', '%závod%',
  '%preteky%', '%maraton%', or known event names, and say which rule you used), commute, trainer.
  distance_m, moving_time_sec, elapsed_time_sec, total_elevation_gain_m, average_speed_mps, pace_sec_per_km.
  avg_power_w, normalized_power_w, intensity_factor (NP/FTP), variability_index (NP/avg power: <=1.05 = steady/
  continuous, higher = surgy or interval work), training_load (TSS), efficiency_factor (NP/avg HR),
  decoupling_pct (aerobic decoupling Pw:HR, 2nd vs 1st half: <5% = durable; this is HR drift, NOT a power drop),
  ftp_at_time, avg_hr, max_hr, lthr, trimp, avg_cadence, calories, feel (1 strong .. 5 weak), rpe (1-10),
  icu_ctl_at_time / icu_atl_at_time (fitness/fatigue on that day), strava_note, next_session (coach's notes).
  raw_metrics JSONB, useful keys: interval_summary (array of strings like '3x 8m 290w' - empty/absent = no
  structured intervals), icu_zone_times (array of {id: 'Z1'..'Z7'/'SS', secs}), icu_hr_zone_times (array of secs
  per HR zone), polarization_index, icu_joules_above_ftp, icu_max_wbal_depletion, icu_rolling_ftp, icu_pm_ftp,
  icu_hrr ({start_bpm, end_bpm, hrr}). JSON access: raw_metrics->>'polarization_index',
  jsonb_array_length(raw_metrics->'interval_summary').

training_blocks - the coach's weekly periodization plan: phase (Base/Build/Peak/...), start_date, end_date,
  target_weekly_load_min/max, primary_focus, is_deload, status (completed/partial/missed/...), notes, sessions JSONB.

athlete_profile - single row (id = 1): current ftp, fthr, weight_kg, max_hr, resting_hr, eftp, w_prime_j, p_max_w.
"""


def _readonly_connection():
    url = load_env().get("READONLY_DATABASE_URL")
    if not url:
        raise RuntimeError("READONLY_DATABASE_URL is not set - run scripts/setup_readonly_role.py")
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor,
                            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c default_transaction_read_only=on")
    conn.set_session(readonly=True)
    return conn


def query_database(sql: str) -> str:
    """Runs one read-only SELECT (or WITH ... SELECT) and returns the rows as a JSON array."""
    sql = sql.strip().rstrip(";").strip()
    if not _STARTS_LIKE_SELECT.match(sql):
        return "Only a single SELECT (or WITH ... SELECT) query is allowed."
    if ";" in sql:
        return "Send one statement per call (no ';' inside the query)."

    try:
        conn = _readonly_connection()
    except Exception as e:
        logger.error(f"history_query: no read-only connection: {e}")
        return f"History database unavailable: {e}"
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchmany(MAX_ROWS + 1)
        conn.rollback()
    except psycopg2.Error as e:
        conn.rollback()
        msg = (e.pgerror or str(e)).strip()
        logger.info(f"history_query: query failed ({msg.splitlines()[0]}): {sql}")
        return f"SQL error: {msg}\nFix the query and try again."
    finally:
        conn.close()

    if not rows:
        return "Query returned no rows."
    truncated = len(rows) > MAX_ROWS
    text = json.dumps([dict(r) for r in rows[:MAX_ROWS]], default=str)
    if len(text) > MAX_CHARS:
        text, truncated = text[:MAX_CHARS], True
    if truncated:
        text += (f"\n[TRUNCATED: more than {MAX_ROWS} rows or {MAX_CHARS} characters. Aggregate in SQL "
                 "(GROUP BY week/month, AVG, COUNT) or narrow the date range instead of reading raw rows.]")
    return text


def data_coverage() -> str:
    """Which date ranges actually hold data, per metric, so the analyst knows up front that e.g.
    HRV only starts in late 2025 instead of reporting an empty summer as 'no change'."""
    conn = _readonly_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 'ctl/atl' AS metric, MIN(day) AS first, MAX(day) AS last, COUNT(*) AS days
                  FROM daily_metrics WHERE ctl IS NOT NULL
                UNION ALL SELECT 'hrv', MIN(day), MAX(day), COUNT(*) FROM daily_metrics WHERE hrv IS NOT NULL
                UNION ALL SELECT 'resting_hr', MIN(day), MAX(day), COUNT(*) FROM daily_metrics WHERE resting_hr IS NOT NULL
                UNION ALL SELECT 'sleep_score', MIN(day), MAX(day), COUNT(*) FROM daily_metrics WHERE sleep_score IS NOT NULL
                UNION ALL SELECT 'sleep_duration', MIN(day), MAX(day), COUNT(*) FROM daily_metrics WHERE sleep_duration_sec IS NOT NULL
                UNION ALL SELECT 'sleep_stages', MIN(day), MAX(day), COUNT(*) FROM daily_metrics WHERE sleep_deep_sec IS NOT NULL
                UNION ALL SELECT 'body_battery', MIN(day), MAX(day), COUNT(*) FROM daily_metrics WHERE body_battery_wake IS NOT NULL
                UNION ALL SELECT 'stress', MIN(day), MAX(day), COUNT(*) FROM daily_metrics WHERE stress_avg IS NOT NULL
                UNION ALL SELECT 'spo2', MIN(day), MAX(day), COUNT(*) FROM daily_metrics WHERE spo2 IS NOT NULL
                UNION ALL SELECT 'weight', MIN(day), MAX(day), COUNT(*) FROM daily_metrics WHERE weight_kg IS NOT NULL
                UNION ALL SELECT 'activities', MIN(start_time)::date, MAX(start_time)::date, COUNT(*)
                  FROM activities WHERE superseded_by IS NULL
                UNION ALL SELECT 'activities with power', MIN(start_time)::date, MAX(start_time)::date, COUNT(*)
                  FROM activities WHERE superseded_by IS NULL AND avg_power_w IS NOT NULL;
            """)
            rows = cur.fetchall()
        conn.rollback()
    finally:
        conn.close()
    return "\n".join(f"- {r['metric']}: {r['first']} to {r['last']} ({r['days']} days/rows with data)" for r in rows)
