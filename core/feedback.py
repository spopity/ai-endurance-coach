"""
The athlete's own feedback, collected by the Telegram bot in a few taps:

- after each hard ride or run, long ride or run, and gym session (asks_rpe()): RPE (1-10) and
  how the legs feel (session_feedback). The RPE is also written to the activity on Intervals.icu
  (icu_rpe), where it gives the session an RPE x minutes load (session_rpe) - the only load a
  gym session really has, as its heart-rate load is near zero,
- every morning after the briefing: body (fresh / normal / sore / very sore / ill) and
  motivation (daily_checkin) - this also covers yesterday's easy sessions, which get no
  question of their own.

It feeds the readiness verdict (signals()), the coach's context (context()) and, for
"ill", the availability file. Numbers come from taps, never from the model.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from core import availability
from core.db import get_connection

logger = logging.getLogger("AI_COACH_V1.feedback")


def query(sql: str, params=()) -> list[dict]:
    """Like core.overview.query - not imported from there, since core.metrics imports this module."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

ASK_WITHIN_HOURS = 36           # sessions older than this aren't asked about any more
NOT_ASKED_TYPES = ("Walk",)
LEGS = {"fresh": "Fresh", "normal": "Normal", "heavy": "Heavy", "sore": "Sore"}
BODY = {"fresh": "Fresh", "normal": "Normal", "sore": "Sore", "very_sore": "Very sore", "ill": "Ill"}
MOTIVATION = {"high": "High", "normal": "Normal", "low": "Low"}
HARD_RIDE_KINDS = ("sst", "vo2")  # core.plan.classify kinds that count as a hard ride
HARD_RUN_Z4_MIN = 10            # a run with this many minutes in HR Z4+ ...
HARD_RUN_Z3_MIN = 25            # ... or this many in Z3+ (long tempo) is a hard run


def _execute(sql: str, params=()):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# --- after a session ---

def is_hard(a: dict) -> bool:
    """A ride classified sweet spot / VO2, a run with real time at or above threshold, an FTP test or a race.
    `a` needs type, name, race, power_zones and hr_zones (raw Intervals.icu zone times)."""
    from core.metrics import FTP_TEST_NAME  # lazy: core.metrics imports this module
    from core.plan import RIDE_TYPES, RUN_TYPES, classify  # lazy: core.plan imports core.overview
    if a.get("race") or FTP_TEST_NAME.search(a.get("name") or ""):
        return a["type"] in RIDE_TYPES | RUN_TYPES
    if a["type"] in RIDE_TYPES:
        return classify(a["type"], a.get("power_zones"), a.get("hr_zones")) in HARD_RIDE_KINDS
    if a["type"] in RUN_TYPES:
        mins = [(s or 0) / 60 for s in (a.get("hr_zones") or [])]
        return sum(mins[3:]) >= HARD_RUN_Z4_MIN or sum(mins[2:]) >= HARD_RUN_Z3_MIN
    return False


def asks_rpe(a: dict) -> bool:
    """Hard sessions, the long ride / long run and gym get their own RPE question."""
    from core.gym import WEIGHTS_TYPES
    from core.plan import LONG_RIDE_MIN, LONG_RUN_MIN, RIDE_TYPES, RUN_TYPES
    minutes = (a.get("moving_time_sec") or 0) / 60
    if a["type"] in WEIGHTS_TYPES:
        return True
    if a["type"] in RIDE_TYPES and minutes >= LONG_RIDE_MIN or a["type"] in RUN_TYPES and minutes >= LONG_RUN_MIN:
        return True
    return is_hard(a)


def _recent_unasked(since: datetime, until: datetime | None = None) -> list[dict]:
    return query("""
        SELECT a.external_id, a.name, a.type, a.start_time, a.moving_time_sec, a.training_load, a.race,
               a.raw_payload->'icu_zone_times' AS power_zones, a.raw_payload->'icu_hr_zone_times' AS hr_zones
        FROM activities a LEFT JOIN session_feedback f ON f.external_id = a.external_id
        WHERE a.superseded_by IS NULL AND f.external_id IS NULL AND a.start_time >= %s AND a.start_time < %s
          AND a.type <> ALL(%s)
        ORDER BY a.start_time;
    """, (since, until or datetime.now(timezone.utc) + timedelta(days=1), list(NOT_ASKED_TYPES)))


def sessions_to_ask() -> list[dict]:
    """Recent hard, long and gym sessions not asked about yet, oldest first. Easy ones are left to
    the next morning's check-in."""
    return [a for a in _recent_unasked(datetime.now(timezone.utc) - timedelta(hours=ASK_WITHIN_HOURS)) if asks_rpe(a)]


def checkin_sessions(day_start: datetime, day_end: datetime) -> list[dict]:
    """The day's sessions that got no question of their own - named in the morning check-in."""
    return [a for a in _recent_unasked(day_start, day_end) if not asks_rpe(a)]


def mark_asked(external_id: str):
    _execute("INSERT INTO session_feedback (external_id) VALUES (%s) ON CONFLICT DO NOTHING;", (external_id,))


def push_rpe(external_id: str, rpe: int) -> bool:
    """Writes the RPE to the activity on Intervals.icu (icu_rpe); best effort."""
    import requests
    from core.db import load_env
    env = load_env()
    try:
        resp = requests.put(f"https://intervals.icu/api/v1/activity/{external_id}", json={"icu_rpe": int(rpe)},
                            auth=("API_KEY", env["INTERVALS_API_KEY"]), timeout=20)
        if resp.status_code >= 300:
            logger.warning(f"RPE not saved on Intervals.icu for {external_id}: {resp.status_code} {resp.text[:120]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"RPE not saved on Intervals.icu for {external_id}: {e}")
        return False


def save_session(external_id: str, rpe: int | None = None, legs: str | None = None):
    if legs is not None and legs not in LEGS:
        raise ValueError(f"Unknown legs value {legs!r}")
    if rpe is not None:
        push_rpe(external_id, rpe)
    _execute("""
        INSERT INTO session_feedback (external_id, rpe, legs, answered_at) VALUES (%s, %s, %s, now())
        ON CONFLICT (external_id) DO UPDATE SET rpe = COALESCE(EXCLUDED.rpe, session_feedback.rpe),
            legs = COALESCE(EXCLUDED.legs, session_feedback.legs), answered_at = now();
    """, (external_id, rpe, legs))


# --- morning check-in ---

def mark_checkin_asked(day: date):
    _execute("INSERT INTO daily_checkin (day) VALUES (%s) ON CONFLICT DO NOTHING;", (day,))


def save_checkin(day: date, body: str | None = None, motivation: str | None = None):
    if body is not None and body not in BODY:
        raise ValueError(f"Unknown body value {body!r}")
    if motivation is not None and motivation not in MOTIVATION:
        raise ValueError(f"Unknown motivation value {motivation!r}")
    _execute("""
        INSERT INTO daily_checkin (day, body, motivation, answered_at) VALUES (%s, %s, %s, now())
        ON CONFLICT (day) DO UPDATE SET body = COALESCE(EXCLUDED.body, daily_checkin.body),
            motivation = COALESCE(EXCLUDED.motivation, daily_checkin.motivation), answered_at = now();
    """, (day, body, motivation))


# --- reading it back ---

def today_checkin(today: date | None = None) -> dict | None:
    rows = query("SELECT body, motivation FROM daily_checkin WHERE day = %s AND answered_at IS NOT NULL;",
                 (today or date.today(),))
    return rows[0] if rows else None


def recent_sessions(limit: int = 5) -> list[dict]:
    rows = query("""
        SELECT f.external_id, f.rpe, f.legs, f.answered_at, a.name, a.type, a.start_time,
               a.raw_payload->'icu_zone_times' AS power_zones, a.raw_payload->'icu_hr_zone_times' AS hr_zones
        FROM session_feedback f JOIN activities a ON a.external_id = f.external_id AND a.superseded_by IS NULL
        WHERE f.answered_at IS NOT NULL ORDER BY a.start_time DESC LIMIT %s;
    """, (limit,))
    from core.plan import classify  # lazy: core.plan imports core.overview
    for r in rows:
        r["kind"] = classify(r["type"], r.pop("power_zones"), r.pop("hr_zones"))
    return rows


def signals(today: date | None = None) -> tuple[list[str], list[str]]:
    """(red reasons, amber reasons) from the athlete's own answers, for core/readiness.py."""
    today = today or date.today()
    red, amber = [], []
    status = availability.day_status(today)
    if status and status["kind"] == "sick":
        red.append("you're ill (no training until you're better)")
    c = today_checkin(today)
    if c:
        if c["body"] == "ill" and not red:
            red.append("you feel ill")
        elif c["body"] == "very_sore":
            amber.append("very sore this morning")
        elif c["body"] == "sore" and c["motivation"] == "low":
            amber.append("sore and low motivation")
    last = next(iter(recent_sessions(1)), None)
    if last and last["start_time"] >= datetime.now(timezone.utc) - timedelta(hours=36):
        if last["legs"] == "sore":
            amber.append("legs sore after the last hard session")
    return red, amber


def gym_load(days: int = 14) -> list[dict]:
    """Gym sessions of the last `days` days with their RPE and session load (RPE x minutes)."""
    from core.gym import WEIGHTS_TYPES
    rows = query("""
        SELECT a.start_time, a.name, a.moving_time_sec, COALESCE(f.rpe, (a.raw_payload->>'icu_rpe')::int) AS rpe
        FROM activities a LEFT JOIN session_feedback f ON f.external_id = a.external_id
        WHERE a.superseded_by IS NULL AND a.type = ANY(%s) AND a.start_time >= now() - %s * interval '1 day'
        ORDER BY a.start_time DESC;
    """, (list(WEIGHTS_TYPES), days))
    return [{"date": f"{r['start_time']:%Y-%m-%d}", "name": r["name"], "minutes": round((r["moving_time_sec"] or 0) / 60),
             "rpe": r["rpe"], "session_load": r["rpe"] * round((r["moving_time_sec"] or 0) / 60) if r["rpe"] else None}
            for r in rows]


def _lifts() -> list[dict]:
    try:
        from core import gym_log
        return gym_log.recent()
    except Exception as e:
        logger.warning(f"Gym log unavailable: {e}")
        return []


def context(today: date | None = None) -> dict:
    """For the coach: this morning's check-in, the last sessions' RPE / legs and the gym load."""
    today = today or date.today()
    c = today_checkin(today)
    return {
        "gym_last_14_days": gym_load(),
        "lifts_logged_last_14_days": _lifts(),
        "morning_checkin": ({"body": BODY.get(c["body"]), "motivation": MOTIVATION.get(c["motivation"])} if c else None),
        "recent_sessions": [{"activity": f"{r['type']} '{r['name']}' {r['start_time']:%Y-%m-%d}", "kind": r["kind"],
                             "rpe": r["rpe"], "legs": LEGS.get(r["legs"])} for r in recent_sessions()],
    }
