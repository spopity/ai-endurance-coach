"""
Syncs the athlete profile (athlete_profile, id=1) from Intervals.icu, so FTP,
LTHR, max HR, resting HR and weight are edited in ONE place (Intervals.icu)
and every agent uses the same values.

- FTP / LTHR / max HR come from the Ride sport settings.
- Weight / resting HR come from the athlete record.
- eFTP / W' / peak power come from the latest wellness day's sportInfo
  (already imported into daily_metrics), so drift between the set FTP and
  Intervals.icu's estimate can be flagged. eFTP never overwrites FTP.

Run: python ingest/import_profile.py
"""
import json
import os
import sys

import psycopg2
import psycopg2.extras
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.db import load_env  # noqa: E402

BASE_URL = "https://intervals.icu"
CYCLING_SETTINGS_TYPE = "Ride"
FIELDS = ("ftp", "fthr", "max_hr", "resting_hr", "weight_kg", "eftp", "w_prime_j", "p_max_w", "eftp_day")


def fetch_athlete(athlete_id: str, api_key: str) -> dict:
    resp = requests.get(f"{BASE_URL}/api/v1/athlete/{athlete_id}", auth=("API_KEY", api_key), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _ride_settings(athlete: dict) -> dict:
    for s in athlete.get("sportSettings") or []:
        if CYCLING_SETTINGS_TYPE in (s.get("types") or []):
            return s
    return {}


def _latest_eftp(cur) -> dict:
    cur.execute("""
        SELECT day, sport_info FROM daily_metrics
        WHERE sport_info IS NOT NULL AND sport_info::text LIKE '%%eftp%%'
        ORDER BY day DESC LIMIT 1;
    """)
    row = cur.fetchone()
    if not row:
        return {}
    info = row["sport_info"] if isinstance(row["sport_info"], list) else json.loads(row["sport_info"])
    ride = next((s for s in info if s.get("type") == CYCLING_SETTINGS_TYPE and s.get("eftp")), None)
    if not ride:
        return {}
    return {"eftp": round(float(ride["eftp"]), 1), "w_prime_j": ride.get("wPrime"),
            "p_max_w": ride.get("pMax"), "eftp_day": row["day"]}


def sync_profile() -> dict:
    """Updates athlete_profile from Intervals.icu. Returns {"changed": {field: (old, new)}, "profile": {...}}."""
    env = load_env(os.path.join(ROOT, ".env"))
    athlete = fetch_athlete(env["INTERVALS_ATHLETE_ID"], env["INTERVALS_API_KEY"])
    ride = _ride_settings(athlete)

    conn = psycopg2.connect(env["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn.cursor() as cur:
            new = {
                "ftp": ride.get("ftp"),
                "fthr": ride.get("lthr"),
                "max_hr": ride.get("max_hr"),
                "resting_hr": athlete.get("icu_resting_hr"),
                "weight_kg": athlete.get("icu_weight"),
                **_latest_eftp(cur),
            }
            # Never blank out a value Intervals.icu doesn't currently provide.
            new = {k: v for k, v in new.items() if v is not None}

            cur.execute(f"SELECT {', '.join(FIELDS)} FROM athlete_profile WHERE id = 1;")
            old = dict(cur.fetchone() or {})
            changed = {k: (old.get(k), v) for k, v in new.items()
                       if old.get(k) is None or str(old.get(k)) != str(v)}

            cols = list(new)
            cur.execute(f"""
                INSERT INTO athlete_profile (id, {', '.join(cols)}, source, synced_at, updated_at)
                VALUES (1, {', '.join(['%s'] * len(cols))}, 'intervals_icu', now(), now())
                ON CONFLICT (id) DO UPDATE SET
                    {', '.join(f'{c} = EXCLUDED.{c}' for c in cols)},
                    source = 'intervals_icu', synced_at = now(),
                    updated_at = CASE WHEN %s THEN now() ELSE athlete_profile.updated_at END;
            """, [new[c] for c in cols] + [bool(changed)])
        conn.commit()
    finally:
        conn.close()
    return {"changed": changed, "profile": new}


def main():
    result = sync_profile()
    print("Profile:", {k: str(v) for k, v in result["profile"].items()})
    if result["changed"]:
        print("Changed:", {k: f"{o} -> {n}" for k, (o, n) in result["changed"].items()})
    else:
        print("No changes.")


if __name__ == "__main__":
    main()
