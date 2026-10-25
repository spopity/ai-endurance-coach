"""
Upcoming training plan for the dashboard: per week the target load range, the
planned session mix (training_blocks.sessions) and, for the current week, how
many sessions of each kind are already done.

Completed rides are classified from Intervals.icu zone times: power zones
when the ride has power, otherwise HR zones. Mixed rides (group rides, MTB
bursts) will occasionally land in the wrong bucket - it's a guide, not a log.
"""
import logging
from datetime import date, timedelta

from core.focus import adjust_focus_text, adjust_sessions, get_focus
from core.gym import WEIGHTS_TYPES, gym_focus  # noqa: F401 - re-exported
from core.overview import cached, query, rnd
from core.races import fetch_upcoming_races

logger = logging.getLogger("AI_COACH_V1.plan")

SESSION_KINDS = ["endurance", "sst", "vo2", "run", "weights"]

RIDE_TYPES = {"Ride", "GravelRide", "MountainBikeRide", "VirtualRide"}
RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}

# Minutes needed for a ride to count as that kind of session.
VO2_MIN = 10        # power Z5 (or HR Z5+)
SST_SS_MIN = 20     # Intervals.icu sweet-spot band
SST_Z4_MIN = 15     # power Z4 (or HR Z4 >= SST_SS_MIN)


def classify(activity_type: str, power_zones, hr_zones) -> str | None:
    if activity_type in RUN_TYPES:
        return "run"
    if activity_type in WEIGHTS_TYPES:
        return "weights"
    if activity_type not in RIDE_TYPES:
        return None

    if power_zones:
        mins = {z["id"]: (z.get("secs") or 0) / 60 for z in power_zones}
        if mins.get("Z5", 0) >= VO2_MIN:
            return "vo2"
        if mins.get("SS", 0) >= SST_SS_MIN or mins.get("Z4", 0) >= SST_Z4_MIN:
            return "sst"
        return "endurance"
    if hr_zones:
        mins = [(s or 0) / 60 for s in hr_zones]
        if sum(mins[4:]) >= VO2_MIN:
            return "vo2"
        if len(mins) > 3 and mins[3] >= SST_SS_MIN:
            return "sst"
    return "endurance"


def _races_by_week() -> dict[date, list[dict]]:
    try:
        races = cached("races", 600, fetch_upcoming_races)
    except Exception as e:
        logger.warning(f"Could not load races for the plan: {e}")
        return {}
    out: dict[date, list[dict]] = {}
    for r in races:
        d = date.fromisoformat(str(r["date"])[:10])
        out.setdefault(d - timedelta(days=d.weekday()), []).append(
            {"name": r.get("name"), "date": d.isoformat(), "priority": r.get("priority")})
    return out


def week_done(monday: date) -> tuple[dict, int]:
    """Sessions done per kind and total load for the week starting `monday`."""
    acts = query("""
        SELECT type, training_load, raw_payload->'icu_zone_times' AS power_zones,
               raw_payload->'icu_hr_zone_times' AS hr_zones
        FROM activities WHERE superseded_by IS NULL AND start_time::date BETWEEN %s AND %s;
    """, (monday, monday + timedelta(days=6)))
    done = dict.fromkeys(SESSION_KINDS, 0)
    for a in acts:
        kind = classify(a["type"], a["power_zones"], a["hr_zones"])
        if kind:
            done[kind] += 1
    return done, round(sum(float(a["training_load"] or 0) for a in acts))


def week_block(monday: date) -> dict | None:
    """The training_blocks row covering the week starting `monday`, with the training focus applied."""
    rows = query("""
        SELECT phase, is_deload, primary_focus, target_weekly_load_min, target_weekly_load_max, sessions
        FROM training_blocks WHERE start_date <= %s AND end_date >= %s ORDER BY start_date LIMIT 1;
    """, (monday + timedelta(days=3), monday + timedelta(days=3)))
    if not rows:
        return None
    block, mode = rows[0], get_focus()["mode"]
    block["sessions"] = adjust_sessions(block["sessions"], block["is_deload"], mode, block["phase"])
    block["primary_focus"] = adjust_focus_text(block["primary_focus"], block["is_deload"], mode, block["phase"])
    return block


def upcoming_plan(weeks: int = 8) -> list[dict]:
    this_monday = date.today() - timedelta(days=date.today().weekday())
    last_sunday = this_monday + timedelta(weeks=weeks, days=-1)
    blocks = query("""
        SELECT start_date, end_date, phase, is_deload, primary_focus,
               target_weekly_load_min, target_weekly_load_max, sessions, notes
        FROM training_blocks WHERE end_date >= %s AND start_date <= %s ORDER BY start_date;
    """, (this_monday, last_sunday))
    done, load_so_far = week_done(this_monday)
    races = _races_by_week()
    mode = get_focus()["mode"]

    out = []
    for i in range(weeks):
        start = this_monday + timedelta(weeks=i)
        block = next((b for b in blocks if b["start_date"] <= start + timedelta(days=3) <= b["end_date"]), None)
        current = i == 0
        out.append({
            "week": start.isoformat(),
            "phase": block["phase"] if block else None,
            "is_deload": bool(block and block["is_deload"]),
            "focus": adjust_focus_text(block["primary_focus"], block["is_deload"], mode, block["phase"]) if block else None,
            "target_min": rnd(block["target_weekly_load_min"], 0) if block else None,
            "target_max": rnd(block["target_weekly_load_max"], 0) if block else None,
            "sessions": {k: int((adjust_sessions(block["sessions"], block["is_deload"], mode, block["phase"]) or {}).get(k, 0))
                         for k in SESSION_KINDS} if block else None,
            "why": block["notes"] if block else None,
            "current": current,
            "done": done if current else None,
            "load": load_so_far if current else None,
            "races": races.get(start, []),
        })
    return out


def plan_summary() -> str:
    """Plain-text plan context for LLM prompts: this week's progress and, when the next session
    may fall in a new week (it's Sunday), that week's focus and sessions."""
    weeks = upcoming_plan(2)
    today = date.today()
    tomorrow = today.fromordinal(today.toordinal() + 1)
    this_week, next_week = weeks[0], weeks[1]

    def describe(w: dict) -> str:
        if not w["sessions"]:
            return "no plan"
        why = f" Why this phase: {w['why']}." if w.get("why") and not w["why"].startswith("projected") else ""
        return (f"{w['phase']}{' (deload - no intervals)' if w['is_deload'] else ''}, focus: {w['focus']} "
                f"Target {w['target_min']:.0f}-{w['target_max']:.0f} TSS.{why}")

    planned, done = this_week["sessions"] or {}, this_week["done"] or {}
    remaining = {k: max(0, planned.get(k, 0) - done.get(k, 0)) for k in planned}
    lines = [
        f"Today is {today:%A %Y-%m-%d}.",
        f"Training focus: {get_focus()['short']}.",
        f"This week (from {this_week['week']}): {describe(this_week)} Load so far {this_week['load']} TSS.",
        "Sessions this week (done/planned): " + ", ".join(f"{k} {done.get(k, 0)}/{v}" for k, v in planned.items())
        + ". Still to do: " + (", ".join(f"{v} {k}" for k, v in remaining.items() if v) or "nothing") + ".",
    ]
    if tomorrow.isoformat() >= next_week["week"]:
        lines.append(f"A NEW week starts tomorrow (from {next_week['week']}) - if the next session isn't today, "
                     f"plan it for that week: {describe(next_week)} "
                     "Planned sessions: " + ", ".join(f"{v} {k}" for k, v in (next_week["sessions"] or {}).items() if v) + ".")
    return "\n".join(lines)
