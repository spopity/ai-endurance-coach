"""
Readiness verdict (green/amber/red) from form, HRV trend, sleep, the injury checks and the
athlete's own feedback (morning check-in, last session's RPE and legs). Shared by the
morning briefing, the coach agents' context, push experiments and the week-ahead schedule,
so every channel judges "can I train hard?" from the same signals.

What each level means for the day (since 2026-11-22 - single signals adjust the session
instead of cancelling it, as coaches do on "meh" days):
- green: the full plan.
- amber: the planned session stays, quality included, about 20% shorter, with a warm-up test
  (15 min easy + the first interval: normal HR and legs -> carry on, otherwise half the reps or
  endurance).
- red: rest or easy only. Red needs a hard stop (illness, very deep fatigue, HRV well below
  normal, a HIGH non-running injury finding) or a cluster of 2+ recovery signals at once.

Illness early warning (core/illness.py, from the night's heart rate, HRV, sleep stress and
breathing rate): "watch" trims the day like a load signal (amber); a "warning", or "watch" with
the athlete's "a bit off" check-in, is red.

Running findings never set the level: they only limit running (`running.limit`), so a running
warning moves the hard work to the bike instead of cancelling it.

The morning check holds for the day: freeze_morning() stores the verdict right after the
morning sync (before the briefing and the re-decided next session), and for the rest of that
day the level is never better than it. A later sync can recalculate the numbers (e.g.
Intervals.icu re-scoring the loads after a resting-HR change moved TSB from -20 to -18) - that
must not turn the day's 60 min run back into 77 min after the athlete got the 60. It can still
get worse during the day (a check-in answer, a session's feedback): the worse of the two wins.
"""
import json
import logging
import os
from datetime import date, datetime, timedelta

from core.feedback import signals as feedback_signals
from core.injury_checks import SLEEP_MIN_HOURS, run_injury_checks
from core.overview import cached, hrv_status, query

logger = logging.getLogger("AI_COACH_V1.readiness")

EMOJI = {"red": "🔴", "amber": "🟠", "green": "🟢"}
RANK = {"green": 0, "amber": 1, "red": 2}
MORNING_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state",
                            "readiness_morning.json")
# One line a day: the morning verdict next to a fresh one at 13:00 (`log_midday`), to measure how
# often the frozen morning decision would have come out differently with the day's later data.
MIDDAY_LOG = os.path.join(os.path.dirname(MORNING_FILE), "readiness_log.jsonl")
# Intervals.icu fills in a provisional resting HR overnight (often 55-59) until the watch's night
# arrives (~50). Within this many bpm of the watch's value, the profile has caught up.
RHR_MATCH_BPM = 2

POOR_SLEEP_SCORE = 70
VERY_SHORT_SLEEP_HOURS = 5.0   # one night this short is a signal on its own
POOR_NIGHTS_SIGNAL = 2         # this many poor nights in a row is a recovery signal
RECOVERY_CLUSTER = 2           # this many recovery signals at once -> red
# Findings already covered by the HRV trend signal (don't count them twice).
HRV_TITLES = ("HRV well below", "HRV trending low", "HRV in normal")


def _poor(n: dict) -> bool:
    return ((n.get("score") is not None and n["score"] < POOR_SLEEP_SCORE)
            or (n.get("hours") is not None and n["hours"] < SLEEP_MIN_HOURS))


def sleep_status(today: date | None = None) -> dict | None:
    """Last nights' sleep: {last_night, poor_streak, signal (str or None), note (str or None)}."""
    today = today or date.today()
    rows = query("""
        SELECT day, sleep_score, sleep_duration_sec FROM daily_metrics
        WHERE day BETWEEN %s AND %s AND (sleep_score IS NOT NULL OR sleep_duration_sec IS NOT NULL)
        ORDER BY day DESC;
    """, (today - timedelta(days=6), today))
    if not rows or rows[0]["day"] < today - timedelta(days=1):
        return None
    nights = [{"day": r["day"], "score": float(r["sleep_score"]) if r["sleep_score"] is not None else None,
               "hours": float(r["sleep_duration_sec"]) / 3600 if r["sleep_duration_sec"] is not None else None}
              for r in rows]
    streak = 0
    for prev, n in zip([None] + nights, nights):
        if prev is not None and (prev["day"] - n["day"]).days != 1 or not _poor(n):
            break
        streak += 1
    last = nights[0]
    signal = note = None
    if last["hours"] is not None and last["hours"] < VERY_SHORT_SLEEP_HOURS:
        signal = f"only {last['hours']:.1f} h sleep"
    elif streak >= POOR_NIGHTS_SIGNAL:
        signal = f"{streak} poor nights in a row"
    elif streak == 1:
        note = ("one poor night - keep the planned session but start with the warm-up test "
                "(15 min easy + the first interval)")
    return {"last_night": {"score": last["score"], "hours": round(last["hours"], 1) if last["hours"] else None},
            "poor_streak": streak, "signal": signal, "note": note}


def illness_status() -> dict | None:
    try:
        from core.illness import current
        return cached("illness", 300, current)
    except Exception as e:
        logger.warning(f"Illness early warning unavailable: {e}")
        return None


def running_limit(findings: list[dict]) -> dict:
    """What the running findings allow: 'none', 'no_hard_run' (caution) or 'easy_short' (high)."""
    run = [f for f in findings if f["area"] == "Running" and f["severity"] in ("high", "caution")]
    if any(f["severity"] == "high" for f in run):
        limit = "easy_short"
        rule = "runs easy and at most 30 min (or swap the run for a ride) - the hard work goes on the bike"
    elif run:
        limit = "no_hard_run"
        rule = "no hard run and no extra running this week - keep runs easy; the quality goes on the bike"
    else:
        limit, rule = "none", None
    return {"limit": limit, "rule": rule, "reasons": [f["title"] for f in run]}


def verdict(tsb: float | None, hrv: dict | None, findings: list[dict] | None = None,
            subjective: tuple[list, list] | None = None, sleep: dict | None = None,
            illness: dict | None = None) -> tuple[str, str]:
    """(level, one-line verdict) - level is 'red', 'amber' or 'green'. `findings` are the injury
    checks' findings (running ones are ignored here, see running_limit), `subjective` is (red, amber)
    reasons from the athlete's own feedback (core/feedback.signals), `sleep` is sleep_status()."""
    red = list((subjective or ([], []))[0])
    recovery = list((subjective or ([], []))[1])   # signals of a body that hasn't recovered
    other = []                                      # load signals: trim the day, don't stop it
    if tsb is not None and tsb <= -30:
        red.append("very deep fatigue")
    elif tsb is not None and tsb <= -20:
        other.append("heavy fatigue")
    if hrv and hrv["status"] == "low":
        red.append("HRV well below normal")
    elif hrv and hrv["status"] == "below":
        recovery.append("HRV below normal")
    if sleep and sleep.get("signal"):
        recovery.append(sleep["signal"])
    for f in findings or []:
        if f["area"] == "Running" or f["title"].startswith(HRV_TITLES):
            continue
        if f["severity"] == "high":
            red.append(f"injury-risk warning: {f['title']}")
        elif f["severity"] == "caution":
            (recovery if f["area"] in ("Recovery", "Subjective") else other).append(f["title"])
    # Illness early warning (core/illness.py). "watch" trims the day as a load-type signal (it
    # shares HRV with the HRV check, so it doesn't count towards the recovery cluster); with the
    # athlete's "a bit off" it is red, and so is a "warning".
    level = (illness or {}).get("level")
    if level == "warning":
        red.append(f"{illness['text']} ({', '.join(illness['signals'])})")
    elif level == "watch":
        if "feeling a bit off" in recovery:
            recovery.remove("feeling a bit off")
            red.append("early illness signs in last night's numbers and you feel a bit off")
        else:
            other.append(f"{illness['text']}: {', '.join(illness['signals'])}")
    if not red and len(recovery) >= RECOVERY_CLUSTER:
        red.append("several recovery signals together")
    if red:
        return "red", "Take it easy - " + ", ".join(red + recovery + other)
    if recovery or other:
        return "amber", "Train, but trim it ~20% and do the warm-up test - " + ", ".join(recovery + other)
    return "green", "Good to go"


def readiness_context(tsb: float | None, hold: bool = True) -> dict:
    """Readiness block for LLM context: verdict, HRV trend, sleep, running limit and the risk findings.
    hold=False ignores the frozen morning verdict (the midday log compares against it)."""
    hrv = hrv_status()
    try:
        injury = cached("injury", 300, run_injury_checks)
    except Exception as e:
        logger.warning(f"Injury checks failed: {e}")
        injury = None
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
    findings = (injury or {}).get("findings", [])
    ill = illness_status()
    level, text = verdict(tsb, hrv, findings, subjective, sleep, ill)
    morning = morning_verdict() if hold else None
    held = bool(morning and RANK.get(morning["level"], 0) > RANK[level])
    if held:
        level, text = morning["level"], f"{morning['verdict']} (this morning's check - it holds for the day)"
    return {
        "level": level,
        "verdict": text,
        "hrv_trend": ({"avg_7d": hrv["avg7"], "baseline": hrv["baseline"], "status": hrv["status"]} if hrv else None),
        "sleep": sleep and {k: v for k, v in sleep.items() if k != "signal"},
        "running": running_limit(findings),
        "illness_warning": ill and ill["level"] != "none" and ill,
        "injury_overall": injury and injury["overall"],
        "injury_flags": [f"{f['severity'].upper()}: {f['title']}" for f in findings
                         if f["severity"] in ("high", "caution")],
        **({"held_from_morning": True} if held else {}),
    }


def _local_today() -> date:
    from core.schedule import TZ
    return datetime.now(TZ).date()


def _local_now() -> str:
    from core.schedule import TZ
    return datetime.now(TZ).isoformat(timespec="minutes")


def morning_verdict(today: date | None = None) -> dict | None:
    """This morning's stored verdict {date, level, verdict, at}, or None (not today's / none yet)."""
    try:
        with open(MORNING_FILE) as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    return m if m.get("date") == (today or _local_today()).isoformat() and m.get("level") in RANK else None


def night_data(today: date | None = None) -> dict:
    """Is last night's data in? {ready, missing: [what isn't], tsb, rhr_watch, rhr_intervals}.
    Ready = today's sleep and HRV from the watch are stored, and Intervals.icu's resting HR has
    replaced its overnight placeholder (within RHR_MATCH_BPM of the watch's) - that resting HR
    re-scores every HR-based session's load, so form (TSB) is only final after it."""
    today = today or _local_today()
    rows = query("SELECT sleep_end, hrv, resting_hr FROM daily_metrics WHERE day = %s;", (today,))
    r = rows[0] if rows else {}
    prof = query("SELECT resting_hr FROM athlete_profile WHERE id = 1;")
    setting = prof[0]["resting_hr"] if prof else None
    watch = r.get("resting_hr")
    missing = []
    if not r.get("sleep_end"):
        missing.append("last night's sleep")
    if r.get("hrv") is None:
        missing.append("last night's HRV")
    if watch is None:
        missing.append("today's resting HR")
    elif setting is not None and abs(float(setting) - float(watch)) > RHR_MATCH_BPM \
            and not _set_by_us(setting):
        missing.append(f"Intervals.icu's updated resting HR (still {setting:.0f} bpm, watch {watch:.0f})")
    return {"ready": not missing, "missing": missing, "tsb": _tsb(),
            "rhr_watch": watch and float(watch), "rhr_intervals": setting and float(setting)}


def _set_by_us(setting) -> bool:
    """Intervals still holds the resting HR the bot set (core/resting_hr.py): steady, not a placeholder."""
    from core.resting_hr import last_set
    s = last_set()
    return bool(s and int(s["value"]) == int(setting))


def freeze_morning(today: date | None = None, force: bool = True) -> dict | None:
    """Stores this morning's verdict (once a day: a later /briefing doesn't replace it).
    force=False stores nothing (returns None) while last night's data isn't in, so an early
    /briefing doesn't fix the day on placeholders; the scheduled check passes force=True at its
    cutoff and the verdict then records what was missing."""
    today = today or _local_today()
    if morning_verdict(today):
        return morning_verdict(today)
    night = night_data(today)
    if not (night["ready"] or force):
        return None
    if os.path.exists(MORNING_FILE):
        os.remove(MORNING_FILE)   # yesterday's: compute today's without its floor
    r = current_readiness()
    m = {"date": today.isoformat(), "level": r["level"], "verdict": r["verdict"],
         "at": _local_now(),
         "missing": night["missing"], "tsb": night["tsb"],
         "rhr_watch": night["rhr_watch"], "rhr_intervals": night["rhr_intervals"]}
    tmp = MORNING_FILE + ".tmp"
    os.makedirs(os.path.dirname(MORNING_FILE), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(m, f)
    os.replace(tmp, MORNING_FILE)
    logger.info(f"Morning readiness for {today}: {m['level']} - {m['verdict']}")
    return m


def _tsb() -> float | None:
    rows = query("SELECT ctl, atl FROM daily_metrics WHERE ctl IS NOT NULL ORDER BY day DESC LIMIT 1;")
    return round(float(rows[0]["ctl"]) - float(rows[0]["atl"]), 1) if rows and rows[0]["atl"] is not None else None


def current_readiness(hold: bool = True) -> dict:
    """readiness_context() with today's form read from the latest daily_metrics row."""
    return readiness_context(_tsb(), hold)


def log_midday(today: date | None = None) -> dict:
    """Appends {date, morning: {...}, midday: {level, verdict, tsb, rhr...}} to MIDDAY_LOG. Changes
    nothing: the record of how often the morning decision and the day's later data disagree."""
    today = today or _local_today()
    night = night_data(today)
    r = current_readiness(hold=False)
    entry = {"date": today.isoformat(), "morning": morning_verdict(today),
             "midday": {"level": r["level"], "verdict": r["verdict"],
                        "at": _local_now(),
                        **{k: night[k] for k in ("tsb", "rhr_watch", "rhr_intervals", "missing")}}}
    os.makedirs(os.path.dirname(MIDDAY_LOG), exist_ok=True)
    with open(MIDDAY_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    m = entry["morning"] or {}
    logger.info(f"Midday readiness for {today}: morning {m.get('level')} / midday {r['level']}")
    return entry
