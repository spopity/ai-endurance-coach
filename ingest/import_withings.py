"""
Backfill/sync: pulls weight measurements from Withings and upserts them into
ai-coach-v1's daily_metrics.weight_kg. Requires ingest/withings_auth.py to
have been run once already (produces withings_tokens.json).

Run: python ingest/import_withings.py [--days 365]
"""
import os
import sys
import json
import argparse
import time
import requests
import psycopg2
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOKEN_PATH = os.path.join(ROOT, "withings_tokens.json")


def load_env():
    env = {}
    with open(os.path.join(ROOT, ".env")) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get_valid_access_token(client_id, client_secret):
    """Withings access tokens expire in ~3 hours - just refresh every run."""
    with open(TOKEN_PATH) as f:
        tokens = json.load(f)

    if "refresh_token" not in tokens:
        raise RuntimeError(
            f"{TOKEN_PATH} has no refresh_token yet. Run ingest/withings_auth.py first."
        )

    data = {
        "action": "requesttoken",
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tokens["refresh_token"],
    }
    resp = requests.post("https://wbsapi.withings.net/v2/oauth2", data=data)
    new_tokens = resp.json().get("body", {})

    if "access_token" not in new_tokens:
        raise RuntimeError(f"Failed to refresh Withings token: {resp.text}")

    # Withings may rotate the refresh_token on every use - persist the new set.
    with open(TOKEN_PATH, "w") as f:
        json.dump(new_tokens, f)

    return new_tokens["access_token"]


def fetch_weight_measurements(access_token, days):
    startdate = int((datetime.now() - timedelta(days=days)).timestamp())
    enddate = int(time.time())

    headers = {"Authorization": f"Bearer {access_token}"}
    data = {
        "action": "getmeas",
        "meastype": 1,  # weight
        "category": 1,  # real measurements, not objectives
        "startdate": startdate,
        "enddate": enddate,
    }
    resp = requests.post("https://wbsapi.withings.net/measure", headers=headers, data=data)
    resp.raise_for_status()
    return resp.json().get("body", {}).get("measuregrps", [])


UPSERT_SQL = """
    INSERT INTO daily_metrics (day, weight_kg, updated_at)
    VALUES (%(day)s, %(weight_kg)s, now())
    ON CONFLICT (day) DO UPDATE SET
        weight_kg = EXCLUDED.weight_kg,
        updated_at = now()
    RETURNING (xmax = 0) AS inserted;
"""


def sync_weight(days: int = 365) -> dict:
    env = load_env()
    client_id = env.get("WITHINGS_CLIENT_ID")
    client_secret = env.get("WITHINGS_CLIENT_SECRET")
    db_url = env["DATABASE_URL"]

    if not client_id or not client_secret:
        raise RuntimeError("WITHINGS_CLIENT_ID / WITHINGS_CLIENT_SECRET not set in .env.")

    access_token = get_valid_access_token(client_id, client_secret)
    measuregrps = fetch_weight_measurements(access_token, days)

    # One weigh-in per day expected; if there are several, keep the latest by timestamp.
    by_day = {}
    for grp in measuregrps:
        if not grp.get("measures"):
            continue
        measure = grp["measures"][0]
        weight_kg = round(measure["value"] * (10 ** measure["unit"]), 2)
        day = datetime.fromtimestamp(grp["date"]).date()
        if day not in by_day or grp["date"] > by_day[day][0]:
            by_day[day] = (grp["date"], weight_kg)

    inserted, updated = 0, 0
    conn = psycopg2.connect(db_url)
    with conn.cursor() as cur:
        for day, (_, weight_kg) in by_day.items():
            cur.execute(UPSERT_SQL, {"day": day, "weight_kg": weight_kg})
            was_inserted = cur.fetchone()[0]
            if was_inserted:
                inserted += 1
            else:
                updated += 1
        conn.commit()
    conn.close()

    return {"fetched": len(by_day), "inserted": inserted, "updated": updated}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()

    print(f"Fetching last {args.days} days of Withings weight data...")
    result = sync_weight(args.days)
    print(f"Fetched {result['fetched']} distinct days of weigh-ins.")
    print(f"\nDone. Inserted: {result['inserted']}  Updated: {result['updated']}")


if __name__ == "__main__":
    main()
