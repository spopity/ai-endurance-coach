"""
Hourly health check of everything that can fail quietly; Telegram only when something goes
wrong or recovers (state in state/health_alerts.json), never repeated while it stays wrong.

Checks:
- every external service in api_calls (Gemini, Claude, Garmin, Intervals.icu, Strava, Ollama...):
  the last 3 hours have >= 3 failures making up half or more of the calls,
- money / auth errors on any call in the last 3 hours (402, 401, 403, exhausted credits, revoked
  tokens) - one is enough, since the fallbacks hide them (e.g. Gemini credits ran out in Nov 2026
  and 67 coach answers quietly came from the fallback),
- the newest backup in /var/backups/ai-coach is older than 30 h,
- no Garmin health day (HRV) for the last 2 days,
- an ai-coach systemd unit in the failed state.

The Intervals.icu API itself has its own 15-minute check (check_intervals_api.py).

Run:
    python scripts/check_health.py [--notify]
"""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.db import load_env  # noqa: E402
from core.notify import notify_telegram  # noqa: E402
from core.overview import query  # noqa: E402
from logging_config import setup_logging  # noqa: E402

logger = logging.getLogger("AI_COACH_V1.health")

STATE_PATH = os.path.join(ROOT, "state", "health_alerts.json")
BACKUP_DIR = "/var/backups/ai-coach"
WINDOW = "3 hours"
MIN_ERRORS = 3
BACKUP_MAX_AGE_H = 30
GARMIN_MAX_AGE_DAYS = 2
MONEY_AUTH = re.compile(r"\b(401|402|403)\b|RESOURCE_EXHAUSTED.*(credit|billing|prepay)|credits? (are )?(depleted|exhausted)"
                        r"|invalid_grant|unauthori[sz]ed|token (has )?expired|quota exceeded", re.IGNORECASE)


def check_services() -> dict:
    out = {}
    rows = query(f"""SELECT service, count(*) n, count(*) FILTER (WHERE NOT ok) bad,
                            (array_agg(left(error, 160) ORDER BY ts DESC) FILTER (WHERE NOT ok))[1] last_error
                     FROM api_calls WHERE ts > now() - interval '{WINDOW}' GROUP BY service;""", ())
    for r in rows:
        if r["bad"] >= MIN_ERRORS and r["bad"] * 2 >= r["n"]:
            out[f"errors:{r['service']}"] = (f"{r['service']}: {r['bad']} of {r['n']} calls failed in the last "
                                            f"{WINDOW} - last error: {r['last_error']}")
    rows = query(f"""SELECT service, component, left(error, 200) error FROM api_calls
                     WHERE ts > now() - interval '{WINDOW}' AND NOT ok AND error IS NOT NULL ORDER BY ts DESC;""", ())
    for r in rows:
        if MONEY_AUTH.search(r["error"] or "") and f"auth:{r['service']}" not in out:
            out[f"auth:{r['service']}"] = (f"{r['service']} ({r['component']}): credits, billing or login problem - "
                                          f"{r['error']}")
    return out


def check_backup() -> dict:
    try:
        files = [os.path.join(BACKUP_DIR, n) for n in os.listdir(BACKUP_DIR) if n.startswith("db-") and n.endswith(".enc")]
    except OSError as e:
        return {"backup": f"backup folder unreadable: {e}"}
    if not files:
        return {"backup": "no backup found in " + BACKUP_DIR}
    age_h = (time.time() - max(os.path.getmtime(f) for f in files)) / 3600
    return {"backup": f"the newest backup is {age_h:.0f} h old"} if age_h > BACKUP_MAX_AGE_H else {}


def check_garmin() -> dict:
    rows = query("SELECT max(day) last FROM daily_metrics WHERE hrv IS NOT NULL;", ())
    last = rows[0]["last"] if rows else None
    from datetime import date
    if not last or (date.today() - last).days > GARMIN_MAX_AGE_DAYS:
        return {"garmin": f"no Garmin health data (HRV) since {last} - readiness runs without it"}
    return {}


def check_units() -> dict:
    try:
        res = subprocess.run(["systemctl", "list-units", "--failed", "--no-legend", "--plain", "ai-coach*"],
                             capture_output=True, text=True, timeout=20)
    except Exception as e:
        return {"systemd": f"couldn't read systemd: {e}"}
    failed = [line.split()[0] for line in res.stdout.splitlines() if line.strip()]
    return {f"unit:{u}": f"{u} failed - journalctl -u {u}" for u in failed}


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true")
    args = ap.parse_args()
    setup_logging("health.log")
    env = load_env()

    problems = {}
    for check in (check_services, check_backup, check_garmin, check_units):
        try:
            problems.update(check())
        except Exception as e:
            logger.error(f"{check.__name__} failed: {e}", exc_info=True)
            problems[f"check:{check.__name__}"] = f"health check {check.__name__} itself failed: {e}"

    before = load_state()
    new = {k: v for k, v in problems.items() if k not in before}
    fixed = [k for k in before if k not in problems]
    for k, v in problems.items():
        logger.warning(f"Problem: {v}")
    if args.notify and new:
        notify_telegram(env, "⚠️ AI coach health check:\n" + "\n".join(f"- {v}" for v in new.values()))
    if args.notify and fixed:
        notify_telegram(env, "✅ Resolved:\n" + "\n".join(f"- {before[k]}" for k in fixed))
    if not problems:
        logger.info("All checks OK.")
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(problems, f, indent=1)
    os.replace(tmp, STATE_PATH)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
