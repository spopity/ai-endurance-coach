"""
Keeps activity names in sync with Strava: Strava is the source of truth for
names, and every differing name is written to Intervals.icu (so the regular
Intervals sync keeps it) and to the local DB.

Activities come in from Garmin via Intervals.icu, so each Strava activity is
matched to a local one by the Strava ID Intervals.icu recorded (when it has
one), otherwise by UTC start time within MATCH_WINDOW_SEC. The name an
activity had before its first rename is kept in activities.name_before_strava.

Requires ingest/strava_auth.py to have been run once (strava_tokens.json); the Strava
client lives in core/strava.py.

Run:
    python scripts/sync_strava_names.py --dry-run   # show what would be renamed
    python scripts/sync_strava_names.py             # rename on Intervals.icu + DB
    python scripts/sync_strava_names.py --notify    # + Telegram summary if anything changed/failed
    python scripts/sync_strava_names.py --full      # whole Strava history instead of the last 6 months

Only the last LOOKBACK_DAYS of Strava are checked by default (~1 Strava request instead of
~19 for the full history); older history was reconciled once by scripts/backfill_strava_history.py.

Exit code: 0 = all matched names in sync, 1 = some renames failed, 2 = error.
"""
import argparse
import bisect
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from core.api_log import trigger  # noqa: E402
from core.db import load_env  # noqa: E402
from core.strava import access_token, fetch_activities, utc_ts  # noqa: E402
from check_intervals_sync import notify_telegram  # noqa: E402

logger = logging.getLogger("AI_COACH_V1.strava_names")

INTERVALS_API = "https://intervals.icu/api/v1"
MATCH_WINDOW_SEC = 120
EXAMPLES_SHOWN = 15
LOOKBACK_DAYS = 183  # ~6 months


def load_local(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, external_id, name, start_time,
                   COALESCE(strava_id, raw_payload->>'strava_id') AS strava_id
            FROM activities WHERE source = 'intervals_icu' ORDER BY start_time;
        """)
        return [
            {"id": r[0], "external_id": r[1], "name": r[2], "ts": r[3].timestamp(), "strava_id": r[4]}
            for r in cur.fetchall()
        ]


def match(strava: list[dict], local: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict]]:
    """Pairs each Strava activity with one local activity. Returns (pairs, unmatched_strava)."""
    by_strava_id = {a["strava_id"]: a for a in local if a["strava_id"]}
    times = [a["ts"] for a in local]
    pairs, unmatched, taken = [], [], set()

    # Explicit ID links first, so time matching can't steal those rows.
    rest = []
    for s in strava:
        a = by_strava_id.get(str(s["id"]))
        if a and a["id"] not in taken:
            pairs.append((s, a))
            taken.add(a["id"])
        else:
            rest.append(s)

    for s in rest:
        ts = utc_ts(s["start_date"])
        lo = bisect.bisect_left(times, ts - MATCH_WINDOW_SEC)
        hi = bisect.bisect_right(times, ts + MATCH_WINDOW_SEC)
        candidates = sorted(
            (a for a in local[lo:hi] if a["id"] not in taken and not a["strava_id"]),
            key=lambda a: abs(a["ts"] - ts),
        )
        if not candidates:
            unmatched.append(s)
            continue
        pairs.append((s, candidates[0]))
        taken.add(candidates[0]["id"])
    return pairs, unmatched


def rename_on_intervals(env: dict, external_id: str, name: str):
    resp = requests.put(
        f"{INTERVALS_API}/activity/{external_id}",
        auth=("API_KEY", env["INTERVALS_API_KEY"]),
        json={"name": name},
        timeout=30,
    )
    resp.raise_for_status()


def apply_local(cur, local_id: int, strava_id: str, name: str | None):
    if name is None:
        cur.execute("UPDATE activities SET strava_id = %s WHERE id = %s;", (strava_id, local_id))
    else:
        cur.execute("""
            UPDATE activities
            SET strava_id = %s, name_before_strava = COALESCE(name_before_strava, name),
                name = %s, updated_at = now()
            WHERE id = %s;
        """, (strava_id, name, local_id))


def sync(conn, env: dict, dry_run: bool, full: bool = False) -> dict:
    after = None if full else datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    strava = fetch_activities(access_token(env), after=after)
    local = load_local(conn)
    pairs, unmatched = match(strava, local)

    renamed, failed, linked = [], [], 0
    with conn.cursor() as cur:
        for s, a in pairs:
            new_name = (s.get("name") or "").strip()
            strava_id = str(s["id"])
            if not new_name or new_name == a["name"]:
                if a["strava_id"] != strava_id and not dry_run:
                    apply_local(cur, a["id"], strava_id, None)
                    linked += 1
                continue
            if dry_run:
                renamed.append((a, new_name))
                continue
            try:
                rename_on_intervals(env, a["external_id"], new_name)
            except Exception as e:
                failed.append((a, new_name, str(e)))
                continue
            apply_local(cur, a["id"], strava_id, new_name)
            conn.commit()
            renamed.append((a, new_name))
        conn.commit()

    return {
        "since": after, "strava": len(strava), "local": len(local), "matched": len(pairs),
        "unmatched": unmatched, "renamed": renamed, "failed": failed, "linked": linked,
    }


def report(result: dict, dry_run: bool) -> list[str]:
    verb = "would rename" if dry_run else "renamed"
    since = f" since {result['since']:%Y-%m-%d}" if result["since"] else " (full history)"
    lines = [
        f"Strava{since} {result['strava']}, DB {result['local']}, matched {result['matched']}, "
        f"unmatched on Strava {len(result['unmatched'])}; {verb} {len(result['renamed'])}, "
        f"failed {len(result['failed'])}"
    ]

    def day(ts):
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")

    for a, name in result["renamed"][:EXAMPLES_SHOWN]:
        lines.append(f"  {day(a['ts'])}: {a['name']!r} -> {name!r}")
    if len(result["renamed"]) > EXAMPLES_SHOWN:
        lines.append(f"  ... and {len(result['renamed']) - EXAMPLES_SHOWN} more")
    for a, name, err in result["failed"][:EXAMPLES_SHOWN]:
        lines.append(f"  failed {day(a['ts'])} {a['external_id']} -> {name!r}: {err[:200]}")
    for s in result["unmatched"][:EXAMPLES_SHOWN]:
        lines.append(f"  no DB match: {s['start_date'][:10]} {s.get('sport_type') or s.get('type')} - {s.get('name')}")
    if len(result["unmatched"]) > EXAMPLES_SHOWN:
        lines.append(f"  ... and {len(result['unmatched']) - EXAMPLES_SHOWN} more unmatched")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync activity names from Strava to Intervals.icu and the DB.")
    parser.add_argument("--dry-run", action="store_true", help="only show what would be renamed")
    parser.add_argument("--notify", action="store_true", help="Telegram summary when names changed or renames failed")
    parser.add_argument("--full", action="store_true", help="check the whole Strava history, not the last 6 months")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    env = load_env()
    conn = psycopg2.connect(env["DATABASE_URL"])
    try:
        result = sync(conn, env, args.dry_run, args.full)
    except Exception as e:
        logger.error(f"Strava name sync failed: {e}", exc_info=True)
        if args.notify:
            notify_telegram(env, f"⚠️ Strava name sync failed: {e}")
        return 2
    finally:
        conn.close()

    summary = "\n".join(report(result, args.dry_run))
    print(summary)
    if args.notify and not args.dry_run and (result["renamed"] or result["failed"]):
        notify_telegram(env, f"🏷️ Strava name sync\n\n{summary}")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    with trigger("name_sync"):  # tags this run's calls on the System page
        sys.exit(main())
