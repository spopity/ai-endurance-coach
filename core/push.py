"""
Push experiments: the athlete says "I can do more" and the coach may go above the plan for a
short, time-boxed window - without touching the plan. training_blocks, its targets and its
verdicts stay exactly as they are; a push only adds a ceiling the coach may train up to, in
metrics_context["push"] (rule 18 in agents/ai_coach.py).

- A push lasts up to MAX_DAYS and ends by itself. It is checked every day: on a hard stop (red
  body, a HIGH finding, illness) it is simply off (allowed_today false, with the reasons) and the
  plan applies; an amber day or a single caution keeps the extra volume and only holds the extra
  intensity (intensity_held_because).
- The ceiling is the weekly load for a CTL ramp of RAMP_BY_LEVEL (the plan aims for 2-3/week),
  never above what the athlete actually did at this fitness in their BENCHMARK_YEARS.
- Each push is an experiment with a result, judged when it ends: "absorbed" (more load than the
  plan and no warning) opens the next level, "too_much" (warning, illness or the athlete's own
  word) goes back to level 1 after COOLDOWN_DAYS, "no_extra" changes nothing.
"""
import json
import logging
from datetime import date, timedelta

from core import availability
from core.db import get_connection
from core.feedback import signals as feedback_signals
from core.injury_checks import run_injury_checks
from core.overview import cached, hrv_status, query
from core.periodization import CTL_GAIN_PER_WEEK, comeback, monday
from core.readiness import illness_status, sleep_status, verdict

logger = logging.getLogger("AI_COACH_V1.push")

KINDS = ("volume", "intensity", "both")
DEFAULT_DAYS, MAX_DAYS = 7, 14
MIN_JUDGED_DAYS = 3                  # a push stopped sooner says nothing, unless it was too much
COOLDOWN_DAYS = 7
RAMP_BY_LEVEL = {1: 4.0, 2: 5.0}     # CTL/week. In the athlete's benchmark years, ramps above ~6 mostly ended in a collapse
# While regaining lost fitness (core/periodization.comeback) the plan itself ramps at 5/week, so a
# push sits above that. Still capped by the benchmark weeks at this CTL and watched by the ramp check.
COMEBACK_RAMP_BY_LEVEL = {1: 6.0, 2: 7.0}
BENCHMARK_YEARS = (2022, 2024)       # years with real training time (the latest two had little: not a fair yardstick)
BENCHMARK_BAND = 5                   # weeks that started within this many CTL of today's
MIN_BENCHMARK_WEEKS = 4
HISTORY_KEPT = 20

# A caution in these areas holds a push. Intensity and cardio drift only hold the intensity part,
# and running only holds extra running: the extra volume goes on the bike.
HOLD_AREAS = {"Load", "Recovery", "Biomechanics", "Subjective"}
NO_PUSH_PHASES = {"Recovery", "Taper", "Race"}
NO_PUSH_STAGES = {"ill", "after_illness", "break", "long_break", "easing_back_in"}
INTENSITY_PHASES = {"Build", "Race prep"}  # Base has its own small dose (core/periodization.py)


class PushError(Exception):
    """A push that can't be started or stopped; the message is meant for the athlete."""


# --- storage ---

def _load() -> dict:
    rows = query("SELECT value FROM athlete_settings WHERE key = 'push';")
    value = (rows[0]["value"] if rows else None) or {}
    return {"active": value.get("active"), "history": value.get("history") or []}


def _save(state: dict):
    state["history"] = state["history"][-HISTORY_KEPT:]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO athlete_settings (key, value, updated_at) VALUES ('push', %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
            """, (json.dumps(state),))
        conn.commit()
    finally:
        conn.close()


# --- the numbers ---

def benchmark(ctl: float) -> dict | None:
    """Weekly load at about this fitness in the benchmark years: {weeks, typical, high, top} (median, p75, p90)."""
    def compute():
        return query("""
            WITH weeks AS (
                SELECT date_trunc('week', start_time)::date AS wk, SUM(COALESCE(training_load, 0)) AS load
                FROM activities
                WHERE superseded_by IS NULL AND EXTRACT(YEAR FROM start_time) BETWEEN %s AND %s
                GROUP BY 1
            )
            SELECT w.load, m.ctl FROM weeks w JOIN daily_metrics m ON m.day = w.wk - 1 WHERE m.ctl IS NOT NULL;
        """, BENCHMARK_YEARS)

    rows = cached("push_benchmark", 86400, compute)
    loads = sorted(float(r["load"]) for r in rows if abs(float(r["ctl"]) - ctl) <= BENCHMARK_BAND)
    if len(loads) < MIN_BENCHMARK_WEEKS:
        return None
    at = lambda p: round(loads[min(len(loads) - 1, int(p * len(loads)))])  # noqa: E731
    return {"years": f"{BENCHMARK_YEARS[0]}-{BENCHMARK_YEARS[1]}", "weeks": len(loads),
            "typical": at(0.5), "high": at(0.75), "top": at(0.9)}


def week_ceiling(ctl: float, level: int, plan_max: float, available_days: int, bench: dict | None,
                 comeback: bool = False) -> int:
    """The most load a pushed week may reach: the ramp's load, within the benchmark, scaled for
    days off. Never below the plan's own maximum - a push can't make the plan smaller."""
    ramp = (COMEBACK_RAMP_BY_LEVEL if comeback else RAMP_BY_LEVEL)[level]
    ramp_load = 7 * ctl + 7 * ramp / CTL_GAIN_PER_WEEK
    if bench:
        ramp_load = min(ramp_load, bench["top"])
    return round(max(ramp_load * available_days / 7, plan_max))


def level_now(history: list[dict], today: date) -> tuple[int, date | None]:
    """(level the next push runs at, date a cooldown ends or None) from the past results."""
    judged = [h for h in history if h["result"] in ("absorbed", "too_much")]
    if not judged:
        return 1, None
    last = judged[-1]
    if last["result"] == "too_much":
        until = date.fromisoformat(last["ended"]) + timedelta(days=COOLDOWN_DAYS)
        return 1, until if until > today else None
    return min(last["level"] + 1, max(RAMP_BY_LEVEL)), None


def judge(load: float, plan_load: float, warnings: list[str], felt_too_much: bool = False) -> str:
    if felt_too_much or warnings:
        return "too_much"
    return "absorbed" if load > plan_load else "no_extra"


# --- today's check ---

def _signals() -> dict:
    """What decides a push today: the body (form, HRV, the athlete's feedback) and the injury checks."""
    rows = query("SELECT ctl, atl FROM daily_metrics WHERE ctl IS NOT NULL ORDER BY day DESC LIMIT 1;")
    tsb = float(rows[0]["ctl"]) - float(rows[0]["atl"]) if rows and rows[0]["atl"] is not None else None
    try:
        subjective = feedback_signals()
    except Exception as e:
        logger.warning(f"Athlete feedback unavailable: {e}")
        subjective = None
    try:
        sleep = sleep_status()
    except Exception as e:
        logger.warning(f"Sleep status unavailable: {e}")
        sleep = None
    level, text = verdict(tsb, hrv_status(), None, subjective, sleep, illness_status())
    return {"body_level": level, "body_text": text,
            "findings": cached("injury", 300, run_injury_checks)["findings"]}


def _warnings(signals: dict, stage: str | None) -> list[str]:
    """Anything that means this load was (or would be) too much."""
    out = [f"injury check: {f['title']}" for f in signals["findings"] if f["severity"] == "high"]
    if signals["body_level"] == "red":
        out.append(signals["body_text"])
    if stage in ("ill", "after_illness"):
        out.append("illness")
    return out


def _week(today: date) -> dict | None:
    """This week's plan, load so far and days available."""
    start = monday(today)
    rows = query("""
        SELECT phase, is_deload, target_weekly_load_min, target_weekly_load_max
        FROM training_blocks WHERE start_date <= %s AND end_date >= %s ORDER BY start_date LIMIT 1;
    """, (today, today))
    if not rows:
        return None
    load = query("""
        SELECT COALESCE(SUM(training_load), 0) AS load FROM activities
        WHERE superseded_by IS NULL AND start_time::date BETWEEN %s AND %s;
    """, (start, start + timedelta(days=6)))[0]["load"]
    ctl = query("SELECT ctl FROM daily_metrics WHERE ctl IS NOT NULL AND day < %s ORDER BY day DESC LIMIT 1;", (start,))
    off = availability.unavailable_days(start, start + timedelta(days=6), availability.load(today))
    return {**rows[0], "load": float(load), "ctl": float(ctl[0]["ctl"]) if ctl else 0.0, "available_days": 7 - len(off)}


def _close(state: dict, today: date, signals: dict, stage: str | None, felt_too_much: bool = False) -> dict:
    """Ends the running push and records how it went."""
    active = state["active"]
    start = date.fromisoformat(active["started"])
    end = min(today, date.fromisoformat(active["until"]))
    days = (end - start).days + 1
    load = float(query("""
        SELECT COALESCE(SUM(training_load), 0) AS load FROM activities
        WHERE superseded_by IS NULL AND start_time::date BETWEEN %s AND %s;
    """, (start, end))[0]["load"])
    warnings = _warnings(signals, stage)
    result = judge(load, active["plan_max"] * days / 7, warnings, felt_too_much)
    if days < MIN_JUDGED_DAYS and result != "too_much":
        result = "stopped_early"
    entry = {"started": active["started"], "ended": end.isoformat(), "kind": active["kind"], "level": active["level"],
             "load": round(load), "plan_load": round(active["plan_max"] * days / 7), "result": result,
             "warnings": warnings + (["the athlete said it was too much"] if felt_too_much else []),
             "note": active.get("note", "")}
    state["active"] = None
    state["history"].append(entry)
    _save(state)
    logger.info(f"Push ended: {entry}")
    return entry


def status(back: dict | None = None, today: date | None = None) -> dict:
    """The push block for the coach. `back` is return_to_training from the metrics context.
    Ends a push whose time is up, so this also records results."""
    today = today or date.today()
    state, signals = _load(), _signals()
    stage = (back or {}).get("stage")
    if state["active"] and date.fromisoformat(state["active"]["until"]) < today:
        _close(state, today, signals, stage)

    level, cooldown = level_now(state["history"], today)
    active = state["active"]
    if active:
        level = active["level"]
    week = _week(today)

    # Since 2026-11-22 only hard stops block a push (red body, a HIGH finding, illness): an amber day
    # or a single caution keeps the extra volume and only switches off the extra intensity.
    blocked = _warnings(signals, stage)
    holds = [f"injury check: {f['title']}" for f in signals["findings"]
             if f["severity"] == "caution" and f["area"] in HOLD_AREAS]
    if signals["body_level"] == "amber":
        holds.append(signals["body_text"])
    if stage in NO_PUSH_STAGES and "illness" not in blocked:
        blocked.append("still easing back in after a break")
    if cooldown and not active:
        blocked.append(f"the last push was too much - next one from {cooldown.isoformat()}")

    out = {"active": bool(active), "level": level, "last_result": state["history"][-1] if state["history"] else None,
           "no_extra_running": any(f["area"] == "Running" and f["severity"] == "caution" for f in signals["findings"])}
    if week:
        if week["is_deload"] or week["phase"] in NO_PUSH_PHASES:
            blocked.append(f"{week['phase'].lower()} week - the rest is part of the plan")
        bench = benchmark(week["ctl"])
        plan_max = float(week["target_weekly_load_max"] or 0)
        back_on = comeback(week["ctl"], monday(today))
        ceiling = week_ceiling(week["ctl"], level, plan_max, week["available_days"], bench, back_on)
        out["comeback"] = back_on
        room = max(0, round(ceiling - week["load"]))
        if not room:
            blocked.append(f"this week is already at the ceiling ({ceiling} TSS)")
        out.update(plan_target_max=round(plan_max), week_ceiling=ceiling, room_left_this_week=room, benchmark=bench)
    else:
        blocked.append("no plan for this week")

    allowed = not blocked
    if active:
        hard_ok = (active["kind"] in ("intensity", "both") and allowed and week and week["phase"] in INTENSITY_PHASES
                   and not holds)
        out.update(kind=active["kind"], started=active["started"], until=active["until"], note=active.get("note", ""),
                   allowed_today=allowed, intensity_allowed=bool(hard_ok))
    out["intensity_held_because"] = holds
    # Only worth offering once the plan's own work for the week is done.
    out["can_offer"] = bool(not active and allowed and week and week["load"] >= float(week["target_weekly_load_min"] or 0))
    out["blocked_because"] = blocked
    return out


# --- start / stop ---

def start(kind: str, days: int = DEFAULT_DAYS, note: str = "", back: dict | None = None, today: date | None = None) -> dict:
    today = today or date.today()
    if kind not in KINDS:
        raise PushError(f"Unknown kind {kind!r} - one of {', '.join(KINDS)}.")
    now = status(back, today)
    if now["active"]:
        raise PushError(f"A push is already running until {now['until']}.")
    if now["blocked_because"]:
        raise PushError("Not today: " + "; ".join(now["blocked_because"]) + ". The plan stays as it is.")
    state = _load()
    days = max(1, min(int(days), MAX_DAYS))
    state["active"] = {"kind": kind, "started": today.isoformat(), "until": (today + timedelta(days=days - 1)).isoformat(),
                       "level": now["level"], "plan_max": now["plan_target_max"], "note": note.strip()}
    _save(state)
    logger.info(f"Push started: {state['active']}")
    return status(back, today)


def stop(felt_too_much: bool = False, back: dict | None = None, today: date | None = None) -> dict:
    today = today or date.today()
    state = _load()
    if not state["active"]:
        raise PushError("No push is running.")
    return _close(state, today, _signals(), (back or {}).get("stage"), felt_too_much)
