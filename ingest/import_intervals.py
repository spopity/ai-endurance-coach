"""
One-shot / re-runnable backfill: pulls activities from Intervals.icu and
upserts them into ai-coach-v1's `activities` table.

Run: python ingest/import_intervals.py [--days 365]
"""
import os
import sys
import json
import argparse
import requests
import psycopg2
import psycopg2.extras
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from field_map import map_activity

BASE_URL = "https://intervals.icu"

UPSERT_SQL = """
    INSERT INTO activities (
        source, external_id, name, type, sub_type, device_name, start_time, timezone,
        commute, race, trainer,
        elapsed_time_sec, moving_time_sec, coasting_time_sec, distance_m,
        total_elevation_gain_m, total_elevation_loss_m, average_speed_mps, max_speed_mps,
        pace_sec_per_km,
        avg_power_w, normalized_power_w, intensity_factor, variability_index, training_load,
        efficiency_factor, decoupling_pct, ftp_at_time, critical_power_w, w_prime_j, p_max_w,
        avg_lr_balance, avg_cadence,
        avg_hr, max_hr, resting_hr_at_time, lthr, trimp, hr_load, hr_load_type,
        icu_ctl_at_time, icu_atl_at_time,
        feel, rpe, calories,
        raw_metrics, raw_payload, updated_at
    ) VALUES (
        %(source)s, %(external_id)s, %(name)s, %(type)s, %(sub_type)s, %(device_name)s,
        %(start_time)s, %(timezone)s, %(commute)s, %(race)s, %(trainer)s,
        %(elapsed_time_sec)s, %(moving_time_sec)s, %(coasting_time_sec)s, %(distance_m)s,
        %(total_elevation_gain_m)s, %(total_elevation_loss_m)s, %(average_speed_mps)s, %(max_speed_mps)s,
        %(pace_sec_per_km)s,
        %(avg_power_w)s, %(normalized_power_w)s, %(intensity_factor)s, %(variability_index)s, %(training_load)s,
        %(efficiency_factor)s, %(decoupling_pct)s, %(ftp_at_time)s, %(critical_power_w)s, %(w_prime_j)s, %(p_max_w)s,
        %(avg_lr_balance)s, %(avg_cadence)s,
        %(avg_hr)s, %(max_hr)s, %(resting_hr_at_time)s, %(lthr)s, %(trimp)s, %(hr_load)s, %(hr_load_type)s,
        %(icu_ctl_at_time)s, %(icu_atl_at_time)s,
        %(feel)s, %(rpe)s, %(calories)s,
        %(raw_metrics)s, %(raw_payload)s, now()
    )
    ON CONFLICT (source, external_id) DO UPDATE SET
        name = EXCLUDED.name, type = EXCLUDED.type, sub_type = EXCLUDED.sub_type,
        device_name = EXCLUDED.device_name, start_time = EXCLUDED.start_time, timezone = EXCLUDED.timezone,
        commute = EXCLUDED.commute, race = EXCLUDED.race, trainer = EXCLUDED.trainer,
        elapsed_time_sec = EXCLUDED.elapsed_time_sec, moving_time_sec = EXCLUDED.moving_time_sec,
        coasting_time_sec = EXCLUDED.coasting_time_sec, distance_m = EXCLUDED.distance_m,
        total_elevation_gain_m = EXCLUDED.total_elevation_gain_m,
        total_elevation_loss_m = EXCLUDED.total_elevation_loss_m,
        average_speed_mps = EXCLUDED.average_speed_mps, max_speed_mps = EXCLUDED.max_speed_mps,
        pace_sec_per_km = EXCLUDED.pace_sec_per_km,
        avg_power_w = EXCLUDED.avg_power_w, normalized_power_w = EXCLUDED.normalized_power_w,
        intensity_factor = EXCLUDED.intensity_factor, variability_index = EXCLUDED.variability_index,
        training_load = EXCLUDED.training_load, efficiency_factor = EXCLUDED.efficiency_factor,
        decoupling_pct = EXCLUDED.decoupling_pct, ftp_at_time = EXCLUDED.ftp_at_time,
        critical_power_w = EXCLUDED.critical_power_w, w_prime_j = EXCLUDED.w_prime_j, p_max_w = EXCLUDED.p_max_w,
        avg_lr_balance = EXCLUDED.avg_lr_balance, avg_cadence = EXCLUDED.avg_cadence,
        avg_hr = EXCLUDED.avg_hr, max_hr = EXCLUDED.max_hr, resting_hr_at_time = EXCLUDED.resting_hr_at_time,
        lthr = EXCLUDED.lthr, trimp = EXCLUDED.trimp, hr_load = EXCLUDED.hr_load, hr_load_type = EXCLUDED.hr_load_type,
        icu_ctl_at_time = EXCLUDED.icu_ctl_at_time, icu_atl_at_time = EXCLUDED.icu_atl_at_time,
        feel = EXCLUDED.feel, rpe = EXCLUDED.rpe, calories = EXCLUDED.calories,
        raw_metrics = EXCLUDED.raw_metrics, raw_payload = EXCLUDED.raw_payload, updated_at = now()
    RETURNING (xmax = 0) AS inserted;
"""


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


def fetch_activities(athlete_id, api_key, days):
    oldest = (date.today() - timedelta(days=days)).isoformat()
    newest = date.today().isoformat()
    url = f"{BASE_URL}/api/v1/athlete/{athlete_id}/activities?oldest={oldest}&newest={newest}"
    resp = requests.get(url, auth=("API_KEY", api_key), timeout=60)
    resp.raise_for_status()
    return resp.json()


def sync_activities(days: int = 365) -> dict:
    """Fetches `days` of activities from Intervals.icu and upserts them.
    Callable in-process (e.g. from app.py's /sync-data), not just via CLI.
    """
    env = load_env(os.path.join(ROOT, ".env"))
    athlete_id = env["INTERVALS_ATHLETE_ID"]
    api_key = env["INTERVALS_API_KEY"]
    db_url = env["DATABASE_URL"]

    raw_activities = fetch_activities(athlete_id, api_key, days)

    conn = psycopg2.connect(db_url)
    try:
        inserted, updated, failed = upsert_activities(conn, raw_activities)
    finally:
        conn.close()

    return {"fetched": len(raw_activities), "inserted": inserted, "updated": updated, "failed": failed}


def upsert_activities(conn, raw_activities: list) -> tuple[int, int, list]:
    """Maps + upserts raw Intervals.icu activity dicts. Returns (inserted, updated, failed).
    Shared by sync_activities and scripts/check_intervals_sync.py --fix."""
    inserted, updated, failed = 0, 0, []
    with conn.cursor() as cur:
        for raw in raw_activities:
            try:
                record = map_activity(raw)
            except Exception as e:
                failed.append((raw.get("id"), str(e)))
                continue

            params = record.model_dump()
            params["raw_metrics"] = json.dumps(params["raw_metrics"], default=str)
            params["raw_payload"] = json.dumps(params["raw_payload"], default=str)

            cur.execute(UPSERT_SQL, params)
            was_inserted = cur.fetchone()[0]
            if was_inserted:
                inserted += 1
            else:
                updated += 1
        conn.commit()
    return inserted, updated, failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()

    print(f"Fetching last {args.days} days of activities...")
    result = sync_activities(args.days)
    print(f"Fetched {result['fetched']} activities from Intervals.icu.")
    print(f"\nDone. Inserted: {result['inserted']}  Updated: {result['updated']}  Failed to map: {len(result['failed'])}")
    if result["failed"]:
        print("Failed activities (id, error):")
        for aid, err in result["failed"][:20]:
            print(f"  {aid}: {err}")


if __name__ == "__main__":
    main()
