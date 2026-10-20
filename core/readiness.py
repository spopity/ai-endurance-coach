"""
Readiness verdict (green/amber/red) from form, HRV trend and the injury checks.
Shared by the morning briefing and the coach agents' context, so every channel
judges "can I train hard?" from the same signals.
"""
import logging

from core.injury_checks import run_injury_checks
from core.overview import cached, hrv_status, query

logger = logging.getLogger("AI_COACH_V1.readiness")

EMOJI = {"red": "🔴", "amber": "🟠", "green": "🟢"}


def verdict(tsb: float | None, hrv: dict | None, injury_overall: str | None) -> tuple[str, str]:
    """(level, one-line verdict) - level is 'red', 'amber' or 'green'."""
    reasons_red, reasons_amber = [], []
    if tsb is not None and tsb <= -30:
        reasons_red.append("very deep fatigue")
    elif tsb is not None and tsb <= -20:
        reasons_amber.append("heavy fatigue")
    if hrv and hrv["status"] == "low":
        reasons_red.append("HRV well below normal")
    elif hrv and hrv["status"] == "below":
        reasons_amber.append("HRV below normal")
    if injury_overall == "high":
        reasons_red.append("injury-risk warning")
    elif injury_overall == "caution":
        reasons_amber.append("some risk flags")
    if reasons_red:
        return "red", "Take it easy - " + ", ".join(reasons_red + reasons_amber)
    if reasons_amber:
        return "amber", "Train, but controlled - " + ", ".join(reasons_amber)
    return "green", "Good to go"


def readiness_context(tsb: float | None) -> dict:
    """Readiness block for LLM context: verdict, HRV trend and the current risk findings."""
    hrv = hrv_status()
    try:
        injury = cached("injury", 300, run_injury_checks)
    except Exception as e:
        logger.warning(f"Injury checks failed: {e}")
        injury = None
    level, text = verdict(tsb, hrv, injury and injury["overall"])
    return {
        "level": level,
        "verdict": text,
        "hrv_trend": ({"avg_7d": hrv["avg7"], "baseline": hrv["baseline"], "status": hrv["status"]} if hrv else None),
        "injury_overall": injury and injury["overall"],
        "injury_flags": [f"{f['severity'].upper()}: {f['title']}" for f in (injury or {}).get("findings", [])
                         if f["severity"] in ("high", "caution")],
    }


def current_readiness() -> dict:
    """readiness_context() with today's form read from the latest daily_metrics row."""
    rows = query("SELECT ctl, atl FROM daily_metrics WHERE ctl IS NOT NULL ORDER BY day DESC LIMIT 1;")
    tsb = float(rows[0]["ctl"]) - float(rows[0]["atl"]) if rows and rows[0]["atl"] is not None else None
    return readiness_context(tsb)
