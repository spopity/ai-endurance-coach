"""
Progression levels: how big the next quality session is, earned from the sessions actually done.

Every quality session type has a ladder of doses (reps x minutes at a fixed intensity band),
like TrainerRoad's progression levels or a coach's "last time you did 3x12, today 3x15". The
plan (core/schedule.py) still decides WHICH type a week gets (sweet spot in Build 1, threshold
in Build 3...); this module decides HOW MUCH of it.

It is stateless, like the plan: every read replays the quality sessions of the last
REPLAY_DAYS days and moves each type's level by what was really done - numbers only:

- What was done comes from Intervals.icu's interval detection on rides ("3x 9m54s 254w" ->
  three ~10 min reps at 90% of the FTP of that day: sweet spot), and from the time in heart-rate
  zones on runs (heart rate lags on short reps, so runs need less of the dose to count). A
  workout pushed from the plan counts as the dose its name says ("Sweet spot 3x15 min"),
  scaled by Intervals.icu's compliance %.
- The session's RPE (the bot's buttons, or Intervals' own RPE) compared with what that type
  should feel like (EXPECTED_RPE).

The rules per session of a type:
- completed (>= 95% of the level's work), RPE at most expected + 1 (or not given) -> one level
  up; well over the level's work -> up to two levels (the athlete showed the capacity),
- completed but RPE expected + 2 or more (a 9 on sweet spot) -> stay,
- partial (60-95% of the work: an amber trim, a bad day) -> stay; two partials in a row on
  the same type, each under 75%, of workouts pushed from the plan -> one level down (a ride
  with a few efforts that was never meant as that session can't push a level down),
- not done for DETRAIN_DAYS -> one level down (two after twice that).

A session counts towards the type whose ladder it best matches; less than MIN_SHARE of the
first level's work isn't a session of that type at all (a few hard surges on an endurance ride).
"""
import logging
import re
from datetime import date, timedelta

logger = logging.getLogger("AI_COACH_V1.progression")

REPLAY_DAYS = 180
DETRAIN_DAYS = 28
COMPLETED = 0.95         # share of the level's work that counts as done (interval detection is ~1-2% short)
PARTIAL_DOWN = 0.75      # two partials in a row below this share step down
MIN_SHARE = 0.6          # below this share of level 1, the session isn't of that type
BIG_DAY = 1.3            # this much over the level's work -> two levels up
RUN_ZONE_CREDIT = 0.8    # runs: heart rate lags on every rep, so zone time under-counts the work
RUN_THRESHOLD_SHARE = 0.2   # a threshold run has at least this share of its time in HR Z4+...
RUN_TEMPO_SHARE = 0.35      # ...a tempo run this share in Z3+ (an easy run drifting into Z3 has less)
EASY_RPE_MARGIN = 3      # an RPE this far under the type's expected RPE: it was an easy session

# type: band of % FTP (rides) or HR zone index from 0 (runs), shortest rep that counts (s),
# expected RPE, and the ladder of (reps, seconds on, seconds easy between).
LADDERS = {
    "tempo": {"sport": "ride", "band": (0.75, 0.87), "min_rep": 300, "rpe": 5, "title": "Tempo",
              "steps": [(2, 720, 300), (2, 900, 300), (2, 1200, 300), (3, 900, 300), (2, 1800, 300), (3, 1200, 300)]},
    "sweet_spot": {"sport": "ride", "band": (0.87, 0.95), "min_rep": 300, "rpe": 6, "title": "Sweet spot",
                   "steps": [(3, 600, 300), (3, 720, 300), (2, 1200, 300), (3, 900, 300), (2, 1500, 300),
                             (3, 1200, 300), (2, 1800, 300)]},
    "threshold": {"sport": "ride", "band": (0.95, 1.06), "min_rep": 240, "rpe": 7, "title": "Threshold",
                  "steps": [(4, 360, 180), (3, 480, 240), (3, 600, 300), (4, 600, 300), (3, 720, 300),
                            (2, 1080, 360), (2, 1200, 300), (3, 900, 300)]},
    "vo2": {"sport": "ride", "band": (1.06, 1.35), "min_rep": 90, "rpe": 8, "title": "VO2max",
            "steps": [(5, 120, 120), (6, 120, 120), (5, 180, 180), (6, 180, 180), (5, 240, 240), (6, 240, 240),
                      (5, 300, 300)]},
    "run_tempo": {"sport": "run", "zones": (2, 2), "rpe": 6, "title": "Tempo",
                  "steps": [(2, 600, 180), (2, 720, 180), (2, 900, 180), (3, 720, 180), (2, 1200, 180), (3, 900, 180)]},
    "run_threshold": {"sport": "run", "zones": (3, 6), "rpe": 7, "title": "Threshold",
                      "steps": [(4, 300, 120), (3, 480, 120), (4, 480, 120), (3, 600, 120), (4, 600, 120),
                                (3, 720, 180), (2, 1200, 180)]},
    "run_hills": {"sport": "run", "zones": (3, 6), "rpe": 8, "title": "Hills",
                  "steps": [(6, 60, 90), (8, 60, 90), (10, 60, 90), (8, 90, 120), (10, 90, 120), (8, 120, 150)]},
}
# Where a type starts with no history: the doses the plan prescribed before levels existed.
START_LEVEL = {"tempo": 1, "sweet_spot": 1, "threshold": 2, "vo2": 2, "run_tempo": 1, "run_threshold": 1,
               "run_hills": 1}
HILLS_NAME = re.compile(r"\bhills?\b", re.IGNORECASE)
INTERVAL_RE = re.compile(r"^\s*(\d+)x\s+(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?\s+(\d+)w\b")


def work(level_type: str, level: int) -> int:
    reps, on, _ = LADDERS[level_type]["steps"][level]
    return reps * on


def top(level_type: str) -> int:
    return len(LADDERS[level_type]["steps"]) - 1


# --- what a session did ---

def parse_intervals(summary) -> list[tuple[int, int, int]]:
    """Intervals.icu interval_summary ("3x 9m54s 254w") -> [(reps, seconds, watts)]; HR-only lines are skipped."""
    out = []
    for line in summary or []:
        m = INTERVAL_RE.match(str(line))
        if m:
            secs = int(m.group(2) or 0) * 3600 + int(m.group(3) or 0) * 60 + int(m.group(4) or 0)
            out.append((int(m.group(1)), secs, int(m.group(5))))
    return out


def ride_work(summary, ftp: float | None) -> dict[str, int]:
    """Seconds of interval work per ride type, each interval in the band its power falls in."""
    out = {}
    if not ftp:
        return out
    for reps, secs, watts in parse_intervals(summary):
        ratio = watts / ftp
        for t, lad in LADDERS.items():
            if lad["sport"] == "ride" and lad["band"][0] <= ratio < lad["band"][1] and secs >= lad["min_rep"]:
                out[t] = out.get(t, 0) + reps * secs
    return out


def run_work(hr_zones, name: str | None) -> dict[str, int]:
    """Seconds of work per run type from the time in HR zones (lag-corrected). An easy run that
    drifts into Z3 isn't a tempo run: the hard time must be a real share of the run."""
    secs = [s or 0 for s in (hr_zones or [])]
    if len(secs) < 4 or not sum(secs):
        return {}
    z3, z4, total = secs[2], sum(secs[3:]), sum(secs)
    if HILLS_NAME.search(name or ""):
        return {"run_hills": round(z4 / RUN_ZONE_CREDIT)}
    out = {}
    if z4 >= RUN_THRESHOLD_SHARE * total:
        out["run_threshold"] = round(z4 / RUN_ZONE_CREDIT)
    if z3 + z4 >= RUN_TEMPO_SHARE * total:
        out["run_tempo"] = round((z3 + z4) / RUN_ZONE_CREDIT)
    return out


def planned_dose(name: str | None) -> tuple[str, int] | None:
    """(type, seconds of work) from a pushed workout's name like 'Sweet spot 3x15 min (AI coach)'."""
    m = re.match(r"(Sweet spot|Threshold run|Threshold|VO2max|Tempo run|Tempo|Hill repeats)\s+(\d+)x(\d+)\s*(min|s)",
                 name or "")
    if not m:
        return None
    t = {"Sweet spot": "sweet_spot", "Threshold": "threshold", "VO2max": "vo2", "Tempo": "tempo",
         "Threshold run": "run_threshold", "Tempo run": "run_tempo", "Hill repeats": "run_hills"}[m.group(1)]
    return t, int(m.group(2)) * int(m.group(3)) * (60 if m.group(4) == "min" else 1)


def session_type(done: dict[str, int], levels: dict[str, int]) -> tuple[str, float] | None:
    """(type, share of that type's current level) a session counts for, or None."""
    best = None
    for t, secs in done.items():
        if secs < MIN_SHARE * work(t, 0):
            continue
        share = secs / work(t, levels.get(t, START_LEVEL[t]))
        # Prefer the more intense type when both match (a threshold run is also tempo time).
        rank = (list(LADDERS).index(t) if LADDERS[t]["sport"] == "ride" else
                {"run_tempo": 0, "run_threshold": 1, "run_hills": 2}[t])
        if best is None or rank > best[2]:
            best = (t, share, rank)
    return (best[0], best[1]) if best else None


# --- the rules ---

def step(level: int, t: str, share: float, rpe: int | None, partials: int,
         planned: bool = True) -> tuple[int, str, int]:
    """(new level, outcome, partials in a row) after one session of type t. Only `planned`
    sessions (pushed from the plan) can step a level down."""
    expected = LADDERS[t]["rpe"]
    if share >= COMPLETED:
        if rpe is not None and rpe >= expected + 2:
            return level, "completed, felt very hard - repeat", 0
        up = 2 if share >= BIG_DAY and (rpe is None or rpe <= expected) else 1
        return min(top(t), level + up), "completed - level up" + (" x2" if up == 2 else ""), 0
    if not planned:
        return level, "partial - repeat", partials
    if share < PARTIAL_DOWN and partials >= 1:
        return max(0, level - 1), "partial twice - level down", 0
    return level, "partial - repeat", partials + 1 if share < PARTIAL_DOWN else partials


def detrain(level: int, days: int) -> int:
    return max(0, level - days // DETRAIN_DAYS) if days >= DETRAIN_DAYS else level


def replay(sessions: list[dict], today: date) -> dict:
    """Levels per type from the quality sessions, oldest first. Each session: day, sport, done
    ({type: seconds}), rpe, and optionally planned ((type, seconds) of a pushed workout) with
    plan_share (Intervals.icu compliance as a share)."""
    levels = dict(START_LEVEL)
    last: dict[str, date] = {}
    partials: dict[str, int] = {}
    log = []
    for s in sorted(sessions, key=lambda s: s["day"]):
        planned = s.get("planned")
        # Time off first: a type not done for weeks is matched against its reduced level.
        now = {t: detrain(levels[t], (s["day"] - last[t]).days) if t in last else levels[t] for t in levels}
        if planned:
            t = planned[0]
            match = (t, s.get("plan_share", 1.0) * planned[1] / work(t, now[t]))
        else:
            match = session_type(s["done"], now)
        if not match:
            continue
        t, share = match
        if s.get("rpe") is not None and s["rpe"] <= LADDERS[t]["rpe"] - EASY_RPE_MARGIN:
            continue   # felt easy: heart-rate zones set too low, or a drift - not a quality dose
        if t in last and (s["day"] - last[t]).days >= DETRAIN_DAYS:
            partials[t] = 0   # an old partial says nothing about today
        levels[t] = now[t]
        before = levels[t]
        levels[t], outcome, partials[t] = step(levels[t], t, share, s.get("rpe"), partials.get(t, 0), bool(planned))
        last[t] = s["day"]
        log.append({"date": s["day"].isoformat(), "type": t, "name": s.get("name"), "share": round(share, 2),
                    "rpe": s.get("rpe"), "level_before": before + 1, "level_after": levels[t] + 1, "outcome": outcome})
    out = {}
    for t in LADDERS:
        lvl = detrain(levels[t], (today - last[t]).days) if t in last else levels[t]
        out[t] = {"level": lvl, "last_done": last[t].isoformat() if t in last else None}
    return {"levels": out, "log": log}


# --- reading it from the database ---

FTP_LOOKBACK_DAYS = 180   # extra ride history read to know how old each ride's FTP was


def training_ftps(rows: list[dict]) -> dict[int, float]:
    """{id(row): the FTP the athlete trained with that day} for the ride rows (oldest first; each
    with type, day, name, ftp_at_time, eftp) - core/metrics.ftp_history: the tested FTP for 8
    weeks, then eFTP. Rides are measured against it, not against a stale Intervals.icu FTP."""
    from core.metrics import ftp_history
    from core.plan import RIDE_TYPES
    rides = [r for r in rows if r["type"] in RIDE_TYPES]
    return {id(r): f for r, f in zip(rides, ftp_history(rides))}

def _sessions(since: date) -> list[dict]:
    from core.overview import query
    from core.plan import RIDE_TYPES, RUN_TYPES
    from core.schedule import TZ
    from core import workouts
    rows = query("""
        SELECT (a.start_time AT TIME ZONE %s)::date AS day, a.type, a.name, a.race, a.ftp_at_time,
               (a.raw_payload->>'icu_rolling_ftp')::float AS eftp,
               a.raw_payload->'interval_summary' AS summary, a.raw_payload->'icu_hr_zone_times' AS hz,
               a.raw_payload->>'paired_event_id' AS paired, a.raw_payload->>'compliance' AS compliance,
               COALESCE(f.rpe, (a.raw_payload->>'icu_rpe')::int) AS rpe
        FROM activities a LEFT JOIN session_feedback f ON f.external_id = a.external_id
        WHERE a.superseded_by IS NULL AND a.type = ANY(%s) AND a.start_time >= %s ORDER BY a.start_time;
    """, (str(TZ), list(RIDE_TYPES | RUN_TYPES), since - timedelta(days=FTP_LOOKBACK_DAYS)))
    from core.metrics import FTP_TEST_NAME
    ftps = training_ftps(rows)
    from core.schedule import DURABILITY_TEST_NAME, RUN_TEST_NAME
    events = workouts.pushed_events()
    out = []
    for r in rows:
        if r["day"] < since:
            continue   # only read for the FTP history
        if (r["race"] or FTP_TEST_NAME.search(r["name"] or "") or RUN_TEST_NAME.search(r["name"] or "")
                or DURABILITY_TEST_NAME.search(r["name"] or "")):
            continue   # tests and races aren't training doses
        ride = r["type"] in RIDE_TYPES
        s = {"day": r["day"], "name": r["name"], "rpe": r["rpe"],
             "done": ride_work(r["summary"], ftps.get(id(r))) if ride else run_work(r["hz"], r["name"])}
        planned = workouts.match(r["paired"], r["compliance"], events) if r["paired"] else None
        dose = planned_dose(planned.get("name")) if planned else None
        if dose and planned.get("compliance") is not None:
            s["planned"], s["plan_share"] = dose, planned["compliance"] / 100
        out.append(s)
    return out


def current(today: date | None = None) -> dict:
    """{levels: {type: {level, last_done}}, log: [...]} - cached for 5 minutes."""
    from core.overview import cached
    today = today or date.today()
    return cached(f"progression:{today}", 300, lambda: replay(_sessions(today - timedelta(days=REPLAY_DAYS)), today))


def levels(today: date | None = None) -> dict[str, int]:
    try:
        return {t: v["level"] for t, v in current(today)["levels"].items()}
    except Exception as e:
        logger.warning(f"Progression levels unavailable, using the start levels: {e}")
        return dict(START_LEVEL)


# --- the prescription ---

def dose(t: str, level: int) -> dict:
    reps, on, off = LADDERS[t]["steps"][max(0, min(level, top(t)))]
    return {"type": t, "reps": reps, "on": on, "off": off, "level": level + 1, "of": top(t) + 1}


def reps_text(d: dict) -> str:
    on = d["on"]
    return f"{d['reps']}x{on // 60} min" if on % 60 == 0 and on >= 120 else f"{d['reps']}x{on} s"


def context(today: date | None = None) -> dict:
    """For the coach: each type's level, its next dose and the last few judgements."""
    try:
        c = current(today)
    except Exception as e:
        logger.warning(f"Progression unavailable: {e}")
        return {}
    return {
        "levels": {t: {"level": f"{v['level'] + 1}/{top(t) + 1}", "next_dose": reps_text(dose(t, v["level"])),
                       "last_done": v["last_done"]} for t, v in c["levels"].items()},
        "recent": c["log"][-6:],
    }
