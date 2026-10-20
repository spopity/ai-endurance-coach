"""
Reconciles the database against Intervals.icu over the FULL history:
activities by ID, wellness by day.

The regular sync only looks back a few days by activity date, so anything
uploaded late (e.g. a holiday ride synced a week later) or lost to a failed
sync never arrives. This script finds those gaps and, with --fix, imports
exactly the missing records.

Run:
    python scripts/check_intervals_sync.py            # report only
    python scripts/check_intervals_sync.py --fix      # import anything missing
    python scripts/check_intervals_sync.py --fix --notify   # + Telegram message if gaps were found

Exit code: 0 = in sync (or every gap fixed), 1 = gaps remain, 2 = error.
Records that exist in the DB but no longer on Intervals.icu are reported only,
never deleted.
"""
import argparse
import logging
import os
import sys
from datetime import date

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ingest"))

from core.api_log import trigger  # noqa: E402
from core.db import load_env  # noqa: E402
from core.notify import notify_telegram  # noqa: E402
from import_intervals import fetch_activities, upsert_activities  # noqa: E402
from import_wellness import fetch_wellness, upsert_wellness  # noqa: E402
from import_profile import sync_profile  # noqa: E402

logger = logging.getLogger("AI_COACH_V1.check_sync")

# Far enough back to cover the whole Intervals.icu history (oldest activity is 2013).
HISTORY_START = date(2000, 1, 1)
EXAMPLES_SHOWN = 10


def _history_days() -> int:
    return (date.today() - HISTORY_START).days


def check_activities(conn, env: dict) -> dict:
    remote = fetch_activities(env["INTERVALS_ATHLETE_ID"], env["INTERVALS_API_KEY"], _history_days())
    with conn.cursor() as cur:
        cur.execute("SELECT external_id FROM activities WHERE source = 'intervals_icu';")
        local = {r[0] for r in cur.fetchall()}
    remote_ids = {str(a["id"]) for a in remote}
    missing = sorted((a for a in remote if str(a["id"]) not in local), key=lambda a: a.get("start_date_local") or "")
    return {
        "remote_count": len(remote),
        "local_count": len(local),
        "missing": missing,
        "db_only": sorted(local - remote_ids),
        "describe": lambda a: f"{a['id']} {str(a.get('start_date_local'))[:10]} {a.get('type')} - {a.get('name')}",
    }


def check_wellness(conn, env: dict) -> dict:
    remote = fetch_wellness(env["INTERVALS_ATHLETE_ID"], env["INTERVALS_API_KEY"], _history_days())
    with conn.cursor() as cur:
        cur.execute("SELECT day FROM daily_metrics;")
        local = {r[0].isoformat() for r in cur.fetchall()}
    remote_days = {str(w["id"]) for w in remote}
    missing = sorted((w for w in remote if str(w["id"]) not in local), key=lambda w: str(w["id"]))
    return {
        "remote_count": len(remote),
        "local_count": len(local),
        "missing": missing,
        "db_only": sorted(local - remote_days),
        "describe": lambda w: str(w["id"]),
    }


def report(name: str, result: dict) -> list[str]:
    lines = [
        f"{name}: Intervals.icu {result['remote_count']}, DB {result['local_count']}, "
        f"missing from DB {len(result['missing'])}, DB-only {len(result['db_only'])}"
    ]
    for item in result["missing"][:EXAMPLES_SHOWN]:
        lines.append(f"  missing: {result['describe'](item)}")
    if len(result["missing"]) > EXAMPLES_SHOWN:
        lines.append(f"  ... and {len(result['missing']) - EXAMPLES_SHOWN} more")
    for item in result["db_only"][:EXAMPLES_SHOWN]:
        lines.append(f"  DB-only (deleted on Intervals.icu?): {item}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the DB has every Intervals.icu activity and wellness day.")
    parser.add_argument("--fix", action="store_true", help="import missing records")
    parser.add_argument("--notify", action="store_true", help="send a Telegram message when gaps are found")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    env = load_env()
    conn = psycopg2.connect(env["DATABASE_URL"])
    try:
        checks = {
            "Activities": (check_activities(conn, env), upsert_activities),
            "Wellness": (check_wellness(conn, env), upsert_wellness),
        }
        lines, gaps_found, gaps_remaining = [], False, False
        for name, (result, upsert) in checks.items():
            lines += report(name, result)
            if not result["missing"]:
                continue
            gaps_found = True
            if args.fix:
                inserted, updated, failed = upsert(conn, result["missing"])
                lines.append(f"  fixed: imported {inserted} (updated {updated}), {len(failed)} failed to map")
                for rid, err in failed[:EXAMPLES_SHOWN]:
                    lines.append(f"  failed: {rid}: {err.splitlines()[0][:200]}")
                gaps_remaining |= bool(failed)
            else:
                gaps_remaining = True
    except Exception as e:
        logger.error(f"Sync check failed: {e}", exc_info=True)
        if args.notify:
            notify_telegram(env, f"⚠️ Intervals.icu sync check failed: {e}")
        return 2
    finally:
        conn.close()

    try:
        profile = sync_profile()
        if profile["changed"]:
            lines.append("Profile updated from Intervals.icu: " + ", ".join(
                f"{k} {old} -> {new}" for k, (old, new) in profile["changed"].items() if k != "eftp_day"))
    except Exception as e:
        logger.warning(f"Profile sync failed: {e}")

    summary = "\n".join(lines)
    print(summary)
    if gaps_found and args.notify:
        status = "gaps found and fixed" if args.fix and not gaps_remaining else "gaps found"
        notify_telegram(env, f"🔄 Intervals.icu sync check: {status}\n\n{summary}")
    return 1 if gaps_remaining else 0


if __name__ == "__main__":
    with trigger("sync_check"):  # tags this run's calls on the System page
        sys.exit(main())
