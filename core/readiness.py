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
"""
import logging
from datetime import date, timedelta

from core.feedback import signals as feedback_signals
from core.injury_checks import SLEEP_MIN_HOURS, run_injury_checks
from core.overview import cached, hrv_status, query

logger = logging.getLogger("AI_COACH_V1.readiness")

EMOJI = {"red": "🔴", "amber": "🟠", "green": "🟢"}

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


def readiness_context(tsb: float | None) -> dict:
    """Readiness block for LLM context: verdict, HRV trend, sleep, running limit and the risk findings."""
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
    }


def current_readiness() -> dict:
    """readiness_context() with today's form read from the latest daily_metrics row."""
    rows = query("SELECT ctl, atl FROM daily_metrics WHERE ctl IS NOT NULL ORDER BY day DESC LIMIT 1;")
    tsb = float(rows[0]["ctl"]) - float(rows[0]["atl"]) if rows and rows[0]["atl"] is not None else None
    return readiness_context(tsb)
