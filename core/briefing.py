"""
Telegram morning briefing.

Facts (readiness, HRV, load vs plan, warnings, race countdown) are computed
deterministically from the same overview the dashboard uses; only "today's
session" comes from the AI Coach agent.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core.api_log import trigger
from core.chat import ask_supervisor
from core.overview import build_overview, clear_cache, query
from core.readiness import EMOJI as READINESS_EMOJI, verdict as readiness_verdict

logger = logging.getLogger("AI_COACH_V1.briefing")

LOCAL_TZ = "Europe/Bratislava"

SUGGESTION_PROMPT = (
    "Morning briefing: recommend TODAY's session in at most 4 short lines - type, duration and "
    "intensity/targets (watts or HR), or rest - based on my current form, HRV, this week's plan and the "
    "injury check. No greeting, no preamble, no data recap."
)


def _form_label(tsb: float) -> str:
    if tsb > 25:
        return "transition"
    if tsb > 5:
        return "fresh"
    if tsb > -10:
        return "grey zone"
    if tsb > -30:
        return "optimal training"
    return "high risk"


def _readiness(ov: dict) -> tuple[str, str]:
    """(emoji, one-line verdict) from form, HRV and the injury checks."""
    f, inj = ov.get("fitness"), ov.get("injury")
    level, text = readiness_verdict(f and f["tsb"], ov.get("hrv"), inj and inj["overall"])
    return READINESS_EMOJI[level], text


def _yesterday_activities() -> list[dict]:
    return query("""
        SELECT name, type, moving_time_sec, training_load FROM activities
        WHERE superseded_by IS NULL
          AND (start_time AT TIME ZONE %s)::date = (NOW() AT TIME ZONE %s)::date - 1
        ORDER BY start_time;
    """, (LOCAL_TZ, LOCAL_TZ))


def _hm(seconds) -> str:
    m = int(seconds or 0) // 60
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m}min"


def build_briefing_facts() -> str:
    """The deterministic part of the briefing, as Markdown."""
    clear_cache()  # always fresh numbers after the pre-briefing sync
    ov = build_overview()
    emoji, verdict = _readiness(ov)
    today = datetime.now(ZoneInfo(LOCAL_TZ)).date()
    lines = [f"☀️ **Good morning - {today:%A %d %b}**", f"{emoji} **{verdict}**", ""]

    f = ov.get("fitness")
    if f:
        lines.append(f"• Form **{f['tsb']:+.0f}** ({_form_label(f['tsb'])}) · Fitness {f['ctl']:.0f} "
                     f"({f['ctl_change_7d']:+.1f} in 7 days) · Fatigue {f['atl']:.0f}")
    hrv = ov.get("hrv")
    if hrv:
        lo, hi = hrv["baseline"] - hrv["sd"] / 2, hrv["baseline"] + hrv["sd"] / 2
        lines.append(f"• HRV last reading **{hrv['latest']:.0f} ms** · 7-day {hrv['avg7']:.0f} "
                     f"(your normal {lo:.0f}-{hi:.0f}) - {hrv['status']}")

    y = _yesterday_activities()
    if y:
        lines.append("• Yesterday: " + "; ".join(
            f"{a['name']} {_hm(a['moving_time_sec'])}" + (f" ({a['training_load']:.0f} TSS)" if a["training_load"] else "")
            for a in y))
    else:
        lines.append("• Yesterday: rest day")

    block, week = ov.get("block"), ov["week"]
    if block:
        lo, hi = block["target_weekly_load_min"], block["target_weekly_load_max"]
        done = block["load_so_far_this_block"]
        end = date.fromisoformat(block["end_date"])
        days_left = (end - today).days + 1
        if done < lo:
            status = f"{lo - done:.0f}-{hi - done:.0f} TSS to go"
        elif done <= hi:
            status = f"in range, {hi - done:.0f} TSS left before the cap"
        else:
            status = f"**{done - hi:.0f} TSS over the plan**"
        lines.append(f"• {block['phase']}{' (deload)' if block['is_deload'] else ''} week: {done:.0f} / {lo:.0f}-{hi:.0f} TSS, "
                     f"{days_left} day{'s' if days_left != 1 else ''} left - {status}")
    else:
        lines.append(f"• This week: {week['load']:.0f} TSS, {week['hours']:.1f} h")

    inj = ov.get("injury")
    drift = (ov.get("athlete") or {}).get("ftp_drift")
    drift_warning = drift and drift["status"] != "ok"
    if (inj and inj.get("top")) or drift_warning:
        lines.append("")
        lines.append("⚠️ **Watch**")
        if drift_warning:
            lines.append(f"• FTP check: {drift['message']}")
        for item in (inj or {}).get("top", []):
            lines.append(f"• {item['title']}")
            if "recovery week" in item["title"]:
                lines.append("  → a deload week (~50-60% load) is due - consider planning it now")

    race = ov.get("next_race")
    if race:
        extra = f", predicted {race['predicted_hours']:.1f} h" if race.get("predicted_hours") else ""
        lines.append("")
        lines.append(f"🏁 **{race['name']}** ({race['priority']} race) in **{race['days_until']} days**{extra}")
    return "\n".join(lines)


async def build_briefing() -> str:
    # DB queries + race/weather lookups are blocking - keep them off the bot's event loop.
    with trigger("briefing"):
        facts = await asyncio.to_thread(build_briefing_facts)
    try:
        with trigger("briefing"):
            _, suggestion = await ask_supervisor(SUGGESTION_PROMPT, agent="ai_coach")
    except Exception as e:
        logger.error(f"Briefing suggestion failed: {e}", exc_info=True)
        suggestion = "_(The coach couldn't be reached for today's suggestion.)_"
    return f"{facts}\n\n🎯 **Today**\n{suggestion.strip()}"

