"""
Sync/backfill: pulls daily health data from the Garmin Connect Personal API and
upserts it into ai-coach-v1's `daily_metrics`. Garmin is the only source of the
health columns (sleep, HRV, resting HR, stress, Body Battery, respiration, SpO2,
steps, daily activity); Intervals.icu still provides the fitness model.

Auth: GARMIN_PERSONAL_API_KEY in .env (Garmin Connect web -> Personal API Key). The key
is exchanged for a 24 h access token; there are no refresh tokens, so a new one is
requested whenever the cached one is close to expiring. Garmin rate-limits the token
endpoint, so the token is kept in state/garmin_token.json and shared by every process
(app restarts, CLI runs) until it expires. The key itself expires on the date chosen
when it was created and must then be re-created.

Run: python ingest/import_garmin.py [--days 365 | --start 2016-01-01]
Each 28-day chunk is committed as soon as it is fetched, so an interrupted backfill
keeps what it did; resume it with --start <the first day it didn't reach>.
"""
import os
import sys
import json
import time
import base64
import argparse
import requests
import psycopg2
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOKEN_PATH = os.path.join(ROOT, "state", "garmin_token.json")

BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_URL = BASE_URL + "/personalapitoken-service/personal-api/personal-api-key/token"
CHUNK_DAYS = 28  # within the range limits of the sleep (31 days) and HRV endpoints

# Garmin's sleep score qualifier -> Intervals.icu's 1-4 sleep_quality scale (1 = best),
# which the existing history uses (score 81 GOOD = 2, score 78 FAIR = 3).
SLEEP_QUALITY = {"EXCELLENT": 1, "GOOD": 2, "FAIR": 3, "POOR": 4}

# Garmin columns written on every upsert (a day Garmin returns overwrites all of them).
COLUMNS = [
    "sleep_score", "sleep_duration_sec", "sleep_quality", "avg_sleeping_hr",
    "sleep_start", "sleep_end", "sleep_deep_sec", "sleep_light_sec", "sleep_rem_sec",
    "sleep_awake_sec", "sleep_unmeasurable_sec", "nap_sec", "sleep_awake_count",
    "sleep_stress", "sleep_respiration", "sleep_respiration_low", "sleep_respiration_high", "sleep_feedback",
    "hrv", "hrv_weekly_avg", "hrv_5min_high", "hrv_status", "hrv_baseline_low", "hrv_baseline_high", "hrv_low_upper",
    "resting_hr", "resting_hr_7d_avg", "min_hr", "max_hr",
    "stress_avg", "stress_max", "stress_rest_sec", "stress_low_sec", "stress_medium_sec", "stress_high_sec",
    "stress_qualifier",
    "body_battery_wake", "body_battery_high", "body_battery_low", "body_battery_charged", "body_battery_drained",
    "respiration", "respiration_low", "respiration_high",
    "spo2", "spo2_lowest", "spo2_sleep_avg",
    "steps", "total_kcal", "active_kcal", "intensity_min_moderate", "intensity_min_vigorous",
    "floors_ascended", "wellness_distance_m",
]
# Only fetched for the newest day of a run, so an older row keeps the value it already has.
KEEP_IF_NULL = ["fitness_age"]

UPSERT_SQL = f"""
    INSERT INTO daily_metrics (day, {", ".join(COLUMNS + KEEP_IF_NULL)}, garmin_payload, garmin_updated_at)
    VALUES (%(day)s, {", ".join(f"%({c})s" for c in COLUMNS + KEEP_IF_NULL)}, %(garmin_payload)s, now())
    ON CONFLICT (day) DO UPDATE SET
        {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS)},
        {", ".join(f"{c} = COALESCE(EXCLUDED.{c}, daily_metrics.{c})" for c in KEEP_IF_NULL)},
        garmin_payload = EXCLUDED.garmin_payload, garmin_updated_at = now()
    RETURNING (xmax = 0) AS inserted;
"""

_token = {"value": None, "exp": 0}  # in-memory copy of TOKEN_PATH


def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _valid(t: dict) -> bool:
    return bool(t.get("value")) and t.get("exp", 0) - time.time() > 600


def _save_token(token: str):
    claims = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
    _token.update(value=token, exp=claims.get("exp", time.time() + 23 * 3600))
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    fd = os.open(TOKEN_PATH + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(_token, f)
    os.replace(TOKEN_PATH + ".tmp", TOKEN_PATH)  # atomic, so a parallel process never reads half a file


def forget_token():
    """Drops the cached token (Garmin answered 401 with it), so the next run requests a new one."""
    _token.update(value=None, exp=0)
    try:
        os.remove(TOKEN_PATH)
    except FileNotFoundError:
        pass


def get_access_token(api_key: str) -> str:
    if _valid(_token):
        return _token["value"]
    try:
        with open(TOKEN_PATH) as f:
            saved = json.load(f)
        if _valid(saved):
            _token.update(saved)
            return _token["value"]
    except (FileNotFoundError, ValueError):
        pass
    resp = requests.post(TOKEN_URL, headers={"Authorization": f"ApiKey {api_key}"}, timeout=30)
    if resp.status_code in (401, 403):
        raise RuntimeError("Garmin rejected GARMIN_PERSONAL_API_KEY (expired or revoked?) - "
                           "create a new key in Garmin Connect -> Personal API Key")
    if resp.status_code == 429:
        raise RuntimeError("Garmin is rate-limiting token requests - try again later")
    resp.raise_for_status()
    _save_token(resp.text.strip().strip('"'))  # returned as plain text, a JWT
    return _token["value"]


def _get(session, path, params=None):
    for attempt in range(4):
        resp = session.get(BASE_URL + path, params=params, timeout=60)
        if resp.status_code == 429 and attempt < 3:
            time.sleep(int(resp.headers.get("Retry-After", 0) or 0) or 10 * (attempt + 1))
            continue
        if resp.status_code == 204 or not resp.content:
            return None
        resp.raise_for_status()
        return resp.json()


def _ms_to_dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


def _pos(v):
    """Garmin marks 'not enough data' with negative values (-1, -2)."""
    return v if v is not None and v >= 0 else None


def _avg_sleeping_hr(sleep: dict, hr_samples: list) -> float | None:
    start, end = sleep.get("sleepStartTimestampGMT"), sleep.get("sleepEndTimestampGMT")
    if not start or not end:
        return None
    values = [bpm for ts, bpm in hr_samples if bpm and start <= ts <= end]
    return round(sum(values) / len(values), 1) if values else None


def _int(v):
    return round(v) if v is not None else None


def map_day(day: str, summary: dict, sleep: dict, hrv: dict, heart: dict, prev_heart: dict,
            spo2: dict, fitness_age: dict) -> dict | None:
    """One `daily_metrics` row from the day's Garmin responses, or None if Garmin has nothing.
    `summary` is the per-day user summary (steps, calories, HR, stress, Body Battery, respiration)."""
    if not any((summary, sleep, hrv, heart, spo2)):
        return None
    summary, sleep, hrv, heart, spo2 = (x or {} for x in (summary, sleep, hrv, heart, spo2))
    if not sleep.get("sleepStartTimestampGMT"):
        sleep = {}  # Garmin returns an empty shell for a night without sleep data

    s, scores, baseline = summary, sleep.get("sleepScores") or {}, hrv.get("baseline") or {}
    samples = (prev_heart or {}).get("heartRateValues") or []
    samples = samples + (heart.get("heartRateValues") or [])

    row = {
        "day": day,
        "sleep_score": (scores.get("overall") or {}).get("value"),
        "sleep_duration_sec": sleep.get("sleepTimeSeconds"),
        "sleep_quality": SLEEP_QUALITY.get((scores.get("overall") or {}).get("qualifierKey")),
        "avg_sleeping_hr": _avg_sleeping_hr(sleep, samples),
        "sleep_start": _ms_to_dt(sleep.get("sleepStartTimestampGMT")),
        "sleep_end": _ms_to_dt(sleep.get("sleepEndTimestampGMT")),
        "sleep_deep_sec": sleep.get("deepSleepSeconds"),
        "sleep_light_sec": sleep.get("lightSleepSeconds"),
        "sleep_rem_sec": sleep.get("remSleepSeconds"),
        "sleep_awake_sec": sleep.get("awakeSleepSeconds"),
        "sleep_unmeasurable_sec": sleep.get("unmeasurableSleepSeconds"),
        "nap_sec": sleep.get("napTimeSeconds"),
        "sleep_awake_count": sleep.get("awakeCount"),
        "sleep_stress": _pos(sleep.get("avgSleepStress")),
        "sleep_respiration": sleep.get("averageRespirationValue"),
        "sleep_respiration_low": sleep.get("lowestRespirationValue"),
        "sleep_respiration_high": sleep.get("highestRespirationValue"),
        "sleep_feedback": sleep.get("sleepScoreFeedback"),

        "hrv": hrv.get("lastNightAvg"),
        "hrv_weekly_avg": hrv.get("weeklyAvg"),
        "hrv_5min_high": hrv.get("lastNight5MinHigh"),
        "hrv_status": hrv.get("status"),
        "hrv_baseline_low": baseline.get("balancedLow"),
        "hrv_baseline_high": baseline.get("balancedUpper"),
        "hrv_low_upper": baseline.get("lowUpper"),

        "resting_hr": s.get("restingHeartRate") or heart.get("restingHeartRate"),
        "resting_hr_7d_avg": s.get("lastSevenDaysAvgRestingHeartRate") or heart.get("lastSevenDaysAvgRestingHeartRate"),
        "min_hr": s.get("minHeartRate") or heart.get("minHeartRate"),
        "max_hr": s.get("maxHeartRate") or heart.get("maxHeartRate"),

        "stress_avg": _pos(s.get("averageStressLevel")),
        "stress_max": _pos(s.get("maxStressLevel")),
        "stress_rest_sec": s.get("restStressDuration"),
        "stress_low_sec": s.get("lowStressDuration"),
        "stress_medium_sec": s.get("mediumStressDuration"),
        "stress_high_sec": s.get("highStressDuration"),
        "stress_qualifier": s.get("stressQualifier"),

        "body_battery_wake": s.get("bodyBatteryAtWakeTime"),
        "body_battery_high": s.get("bodyBatteryHighestValue"),
        "body_battery_low": s.get("bodyBatteryLowestValue"),
        "body_battery_charged": s.get("bodyBatteryChargedValue"),
        "body_battery_drained": s.get("bodyBatteryDrainedValue"),

        "respiration": s.get("avgWakingRespirationValue"),
        "respiration_low": s.get("lowestRespirationValue"),
        "respiration_high": s.get("highestRespirationValue"),

        "spo2": spo2.get("averageSpO2"),
        "spo2_lowest": spo2.get("lowestSpO2"),
        "spo2_sleep_avg": spo2.get("avgSleepSpO2"),

        "steps": s.get("totalSteps"),
        "total_kcal": _int(s.get("totalKilocalories")),
        "active_kcal": _int(s.get("activeKilocalories")),
        "intensity_min_moderate": s.get("moderateIntensityMinutes"),
        "intensity_min_vigorous": s.get("vigorousIntensityMinutes"),
        "floors_ascended": _int(s.get("floorsAscended")),
        "wellness_distance_m": s.get("wellnessDistanceMeters"),

        "fitness_age": (fitness_age or {}).get("fitnessAge"),
    }
    if all(row[c] is None for c in COLUMNS):
        return None  # Garmin answers days before the watch with empty shells - don't null out older data
    # Raw responses, minus the intraday sample arrays (HR every 2 min, SpO2 readings) -
    # those are only used above to derive the daily values.
    row["garmin_payload"] = {
        "summary": summary, "sleep": sleep, "hrv": hrv,
        "heart_rate": {k: v for k, v in heart.items() if k not in ("heartRateValues", "heartRateValueDescriptors")},
        "spo2": {k: v for k, v in spo2.items() if not isinstance(v, list)},
        **({"fitness_age": fitness_age} if fitness_age else {}),
    }
    return row


def fetch_days(session, start: date, end: date, throttle: float = 0.0):
    """Fetches and maps every day in [start, end], yielding (chunk_end, rows) per 28-day chunk.
    Sleep and HRV come from range endpoints per chunk; the user summary (has Garmin's exact
    Body Battery at wake), heart rate samples (for sleeping HR) and SpO2 only exist per day."""
    prev_heart = _get(session, "/wellness-service/wellness/dailyHeartRate", {"date": (start - timedelta(days=1)).isoformat()})
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), end)
        a, b = chunk_start.isoformat(), chunk_end.isoformat()
        sleeps = _get(session, "/sleep-service/sleep/dailySleepSummariesByDate", {"startDate": a, "endDate": b}) or []
        hrvs = (_get(session, f"/hrv-service/hrv/daily/{a}/{b}") or {}).get("hrvSummaries") or []
        sleeps = {x.get("calendarDate"): x for x in sleeps if isinstance(x, dict)}
        hrvs = {x.get("calendarDate"): x for x in hrvs if isinstance(x, dict)}

        rows, day = [], chunk_start
        while day <= chunk_end:
            d = day.isoformat()
            summary = _get(session, "/usersummary-service/usersummary/daily", {"calendarDate": d})
            heart = _get(session, "/wellness-service/wellness/dailyHeartRate", {"date": d})
            spo2 = _get(session, f"/wellness-service/wellness/daily/spo2acclimation/{d}")
            fitness_age = _get(session, f"/fitnessage-service/fitnessage/v2/{d}") if day == end else None
            row = map_day(d, summary, sleeps.get(d), hrvs.get(d), heart, prev_heart, spo2, fitness_age)
            if row:
                rows.append(row)
            prev_heart = heart
            day += timedelta(days=1)
            if throttle:
                time.sleep(throttle)
        yield chunk_end, rows
        chunk_start = chunk_end + timedelta(days=1)


def upsert_days(conn, rows: list) -> tuple[int, int]:
    inserted = updated = 0
    with conn.cursor() as cur:
        for row in rows:
            params = dict(row, garmin_payload=json.dumps(row["garmin_payload"], default=str))
            cur.execute(UPSERT_SQL, params)
            if cur.fetchone()[0]:
                inserted += 1
            else:
                updated += 1
        conn.commit()
    return inserted, updated


def sync_garmin(days: int = 365, throttle: float = 0.0, start: date | None = None, progress=None) -> dict:
    """Fetches the last `days` (including today), or everything from `start`, from Garmin and
    upserts it, committing each 28-day chunk. Callable in-process (core/chat.run_sync).
    `progress(chunk_end, totals)` is called after each commit."""
    env = load_env(os.path.join(ROOT, ".env"))
    if not env.get("GARMIN_PERSONAL_API_KEY"):
        raise RuntimeError("GARMIN_PERSONAL_API_KEY is not set in .env")
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + get_access_token(env["GARMIN_PERSONAL_API_KEY"])

    end = date.today()
    totals = {"fetched": 0, "inserted": 0, "updated": 0}
    conn = psycopg2.connect(env["DATABASE_URL"])
    try:
        for chunk_end, rows in fetch_days(session, start or end - timedelta(days=days - 1), end, throttle):
            inserted, updated = upsert_days(conn, rows)
            totals["fetched"] += len(rows)
            totals["inserted"] += inserted
            totals["updated"] += updated
            if progress:
                progress(chunk_end, totals)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            forget_token()  # revoked or invalid: the next run asks for a fresh one
        raise
    finally:
        conn.close()
    return totals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--start", type=date.fromisoformat, help="first day (YYYY-MM-DD), instead of --days")
    parser.add_argument("--throttle", type=float, default=0.2, help="seconds between days (be gentle on backfills)")
    args = parser.parse_args()

    first = args.start or date.today() - timedelta(days=args.days - 1)
    span = (date.today() - first).days + 1
    print(f"Fetching health data from Garmin, {first} to today ({span} days)...", flush=True)
    started = time.time()

    def progress(chunk_end, totals):
        done = (chunk_end - first).days + 1
        left = (time.time() - started) / done * (span - done)
        print(f"  committed up to {chunk_end}: {done}/{span} days, {totals['fetched']} with data, "
              f"~{left / 60:.0f} min left", flush=True)

    result = sync_garmin(args.days, args.throttle, start=args.start, progress=progress)
    print(f"\nDone. Inserted: {result['inserted']}  Updated: {result['updated']}")


if __name__ == "__main__":
    main()
