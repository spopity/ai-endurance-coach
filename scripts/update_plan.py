"""
Recomputes the adaptive training plan (core/periodization.py) from the training
actually done and rewrites training_blocks. Runs nightly after the Intervals.icu
sync; safe to run any time.

Run: python scripts/update_plan.py [--dry-run] [--notify]
  --dry-run  print the replay and the projection, write nothing
  --notify   send a Telegram message when this week's phase changed
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.db import get_connection, load_env  # noqa: E402
from core.notify import notify_telegram  # noqa: E402
from core.periodization import compute_plan, update_plan  # noqa: E402


def label(r: dict | None) -> str:
    return (r["phase"] + (" (deload)" if r["is_deload"] and r["phase"] != "Recovery" else "")) if r else "no plan"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    try:
        if args.dry_run:
            rows = compute_plan(conn)
        else:
            result = update_plan(conn)
            rows = result["rows"]
    finally:
        conn.close()

    for r in rows:
        actual = "" if r["actual_load"] is None else f"{r['actual_load']:>4}"
        print(f"{r['start_date']}  {r['phase']:8} {r['target_weekly_load_min']:>4}-{r['target_weekly_load_max']:<4} "
              f"{actual:>4}  {r['status'] or '':10} {r['notes']}")
    if args.dry_run:
        return

    before, now = result["before"], result["now"]
    print(f"\nThis week: {label(before)} -> {label(now)} ({now['notes']})")
    if args.notify and before and (before["phase"], before["is_deload"]) != (now["phase"], now["is_deload"]):
        notify_telegram(load_env(), (
            f"📅 Training plan adapted: this week is now {label(now)} (was {label(before)}).\n"
            f"Why: {now['notes']}.\n"
            f"Target {now['target_weekly_load_min']:.0f}-{now['target_weekly_load_max']:.0f} TSS. {now['primary_focus']}"))


if __name__ == "__main__":
    main()
