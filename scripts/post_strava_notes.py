"""
Posts coach notes into the Strava descriptions of new activities (any type) (core/strava_notes.py).
Run every 10 minutes by deploy/ai-coach-strava-notes.timer; activities that aren't on
Strava yet are retried on the next run.

Run:
    python scripts/post_strava_notes.py --dry-run              # preview pending notes, post nothing
    python scripts/post_strava_notes.py                        # post pending notes
    python scripts/post_strava_notes.py --activity i123456789  # (re)post one activity, any age
    python scripts/post_strava_notes.py --activity i123456789 --dry-run

Exit code: 0 = ok (or nothing to do), 1 = some notes failed, 2 = error.
"""
import argparse
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.api_log import trigger  # noqa: E402
from core import strava  # noqa: E402
from core.strava_notes import load_activity, post_note, post_pending, require_write_scope  # noqa: E402

logger = logging.getLogger("AI_COACH_V1.post_strava_notes")


def main() -> int:
    parser = argparse.ArgumentParser(description="Post coach notes to Strava activity descriptions.")
    parser.add_argument("--dry-run", action="store_true", help="show the new descriptions, post nothing")
    parser.add_argument("--activity", help="Intervals.icu activity id (e.g. i123456789) to (re)post, regardless of age")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        if args.activity:
            a = load_activity(args.activity)
            if not a:
                print(f"Activity {args.activity} not found.")
                return 2
            if not args.dry_run:
                require_write_scope()
            results = [post_note(a, strava.access_token(), args.dry_run)]
        else:
            results = post_pending(args.dry_run)
    except Exception as e:
        logger.error(f"Strava notes failed: {e}", exc_info=True)
        return 2

    for line in results:
        print(line)
    return 1 if any(": failed" in r for r in results) else 0


if __name__ == "__main__":
    with trigger("strava_note"):  # tags this run's calls on the System page
        sys.exit(main())
