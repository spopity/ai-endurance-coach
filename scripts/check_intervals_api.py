"""
Health check for the Intervals.icu API: one lightweight request to the athlete
endpoint, logged to logs/intervals_health.log on every run.

Telegram is only messaged when the state changes - once when the API goes down
(no response, or a status other than 200) and once when it recovers - so a long
outage doesn't send a message every run. The last state lives in
state/intervals_health.json.

Run:
    python scripts/check_intervals_api.py              # check + log
    python scripts/check_intervals_api.py --notify     # + Telegram on down/recovered

Exit code: 0 = API OK, 1 = API down / not OK.
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.api_log import trigger  # noqa: E402
from core.db import load_env  # noqa: E402
from core.notify import notify_telegram  # noqa: E402
from logging_config import setup_logging  # noqa: E402

logger = logging.getLogger("AI_COACH_V1.intervals_health")

BASE_URL = "https://intervals.icu"
TIMEOUT_SECONDS = 20
STATE_PATH = os.path.join(ROOT, "state", "intervals_health.json")


def check_api(env: dict) -> tuple[bool, str]:
    """Returns (ok, detail). OK means the API answered with HTTP 200."""
    url = f"{BASE_URL}/api/v1/athlete/{env['INTERVALS_ATHLETE_ID']}"
    started = time.monotonic()
    try:
        resp = requests.get(url, auth=("API_KEY", env["INTERVALS_API_KEY"]), timeout=TIMEOUT_SECONDS)
    except requests.Timeout:
        return False, f"no response within {TIMEOUT_SECONDS}s"
    except requests.RequestException as e:
        return False, f"request failed: {e}"
    elapsed_ms = round((time.monotonic() - started) * 1000)
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code} in {elapsed_ms} ms: {resp.text[:200].strip()}"
    return True, f"HTTP 200 in {elapsed_ms} ms"


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ok": True}


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the Intervals.icu API responds with HTTP 200.")
    parser.add_argument("--notify", action="store_true", help="send a Telegram message when the API goes down or recovers")
    args = parser.parse_args()
    setup_logging("intervals_health.log")

    env = load_env()
    ok, detail = check_api(env)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    previous = load_state()

    if ok:
        logger.info(f"Intervals.icu API OK: {detail}")
        if not previous.get("ok", True):
            down_since = previous.get("down_since", "unknown")
            logger.info(f"Intervals.icu API recovered (down since {down_since}).")
            if args.notify:
                notify_telegram(env, f"✅ Intervals.icu API is back: {detail}\nDown since {down_since} (UTC).")
        save_state({"ok": True, "last_ok": now})
        return 0

    logger.error(f"Intervals.icu API NOT OK: {detail}")
    if previous.get("ok", True):
        if args.notify:
            notify_telegram(env, f"⚠️ Intervals.icu API is not responding correctly: {detail}\n"
                                 "Syncs will fail until it's back - I'll message you when it recovers.")
        save_state({"ok": False, "down_since": now, "last_ok": previous.get("last_ok"), "detail": detail})
    else:
        save_state({**previous, "detail": detail})
    return 1


if __name__ == "__main__":
    with trigger("health_check"):  # tags this run's calls on the System page
        sys.exit(main())
