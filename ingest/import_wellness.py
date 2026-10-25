"""
One-shot / re-runnable backfill: pulls wellness records from Intervals.icu
and upserts them into ai-coach-v1's `daily_metrics` table. Only the fitness model,
vo2max, blood pressure, body and subjective fields - the health columns are
Garmin's (ingest/import_garmin.py) and are never written here.

Run: python ingest/import_wellness.py [--days 365]
"""
import os
import sys
import json
import argparse
import requests
import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from field_map_wellness import map_wellness

BASE_URL = "https://intervals.icu"

UPSERT_SQL = """
    INSERT INTO daily_metrics (
        day, ctl, atl, ramp_rate,
        systolic, diastolic, vo2max,
        soreness, fatigue, stress, mood, motivation, readiness, injury,
        weight_kg, body_fat_pct, kcal_consumed,
        comments, sport_info, raw_payload, updated_at
    ) VALUES (
        %(day)s, %(ctl)s, %(atl)s, %(ramp_rate)s,
        %(systolic)s, %(diastolic)s, %(vo2max)s,
        %(soreness)s, %(fatigue)s, %(stress)s, %(mood)s, %(motivation)s, %(readiness)s, %(injury)s,
        %(weight_kg)s, %(body_fat_pct)s, %(kcal_consumed)s,
        %(comments)s, %(sport_info)s, %(raw_payload)s, now()
    )
    ON CONFLICT (day) DO UPDATE SET
        ctl = EXCLUDED.ctl, atl = EXCLUDED.atl, ramp_rate = EXCLUDED.ramp_rate,
        systolic = EXCLUDED.systolic, diastolic = EXCLUDED.diastolic, vo2max = EXCLUDED.vo2max,
        soreness = EXCLUDED.soreness, fatigue = EXCLUDED.fatigue, stress = EXCLUDED.stress,
        mood = EXCLUDED.mood, motivation = EXCLUDED.motivation, readiness = EXCLUDED.readiness,
        injury = EXCLUDED.injury,
        weight_kg = EXCLUDED.weight_kg, body_fat_pct = EXCLUDED.body_fat_pct,
        kcal_consumed = EXCLUDED.kcal_consumed,
        comments = EXCLUDED.comments, sport_info = EXCLUDED.sport_info,
        raw_payload = EXCLUDED.raw_payload, updated_at = now()
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


def fetch_wellness(athlete_id, api_key, days):
    from datetime import date, timedelta
    oldest = (date.today() - timedelta(days=days)).isoformat()
    newest = date.today().isoformat()
    url = f"{BASE_URL}/api/v1/athlete/{athlete_id}/wellness.json?oldest={oldest}&newest={newest}"
    resp = requests.get(url, auth=("API_KEY", api_key), timeout=60)
    resp.raise_for_status()
    return resp.json()


def sync_wellness(days: int = 365) -> dict:
    """Fetches `days` of wellness records from Intervals.icu and upserts them.
    Callable in-process (e.g. from app.py's /sync-data), not just via CLI.
    """
    env = load_env(os.path.join(ROOT, ".env"))
    athlete_id = env["INTERVALS_ATHLETE_ID"]
    api_key = env["INTERVALS_API_KEY"]
    db_url = env["DATABASE_URL"]

    raw_records = fetch_wellness(athlete_id, api_key, days)

    conn = psycopg2.connect(db_url)
    try:
        inserted, updated, failed = upsert_wellness(conn, raw_records)
    finally:
        conn.close()

    return {"fetched": len(raw_records), "inserted": inserted, "updated": updated, "failed": failed}


def upsert_wellness(conn, raw_records: list) -> tuple[int, int, list]:
    """Maps + upserts raw Intervals.icu wellness dicts. Returns (inserted, updated, failed).
    Shared by sync_wellness and scripts/check_intervals_sync.py --fix."""
    inserted, updated, failed = 0, 0, []
    with conn.cursor() as cur:
        for raw in raw_records:
            try:
                record = map_wellness(raw)
            except Exception as e:
                failed.append((raw.get("id"), str(e)))
                continue

            params = record.model_dump(mode="json")
            params["sport_info"] = json.dumps(params["sport_info"], default=str)
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

    print(f"Fetching last {args.days} days of wellness data...")
    result = sync_wellness(args.days)
    print(f"Fetched {result['fetched']} wellness records from Intervals.icu.")
    print(f"\nDone. Inserted: {result['inserted']}  Updated: {result['updated']}  Failed to map: {len(result['failed'])}")
    if result["failed"]:
        print("Failed records (day, error):")
        for day, err in result["failed"][:20]:
            print(f"  {day}: {err}")


if __name__ == "__main__":
    main()
