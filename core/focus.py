"""
Training focus, switched from the dashboard: 'cycling' (default - peak for cycling,
maximise FTP, running is maintenance) or 'dual' (winter dual-sport - cycling and
running both primary). The stored training_blocks plan stays untouched; dual mode
is applied on top of it when the plan is read (session mix + run focus), so
switching back restores the cycling plan exactly.
"""
import json
from datetime import date

from core.db import get_connection

DEFAULT = "cycling"

MODES = {
    "cycling": {
        "label": "Cycling focus",
        "primary_goal": "Peak for cycling by maximizing FTP growth through properly periodized training.",
        "secondary_goal": "Maintain baseline running fitness and structural strength.",
        "running_rule": (
            "**Running Volume (Summer Constraints):** Running is strictly for maintenance. During the summer, "
            "limit running suggestions to a maximum of one 5km run per week. If a run has already been completed "
            "in the last 7 days, do not prescribe another."
        ),
        "short": "cycling first (maximise FTP); running is maintenance only",
    },
    "dual": {
        "label": "Dual-sport (bike + run)",
        "primary_goal": (
            "Dual-sport winter block: build cycling AND running fitness together, both sports are equally "
            "important. Keep FTP-supporting bike work (sweet spot/threshold) but at slightly lower bike volume."
        ),
        "secondary_goal": "Structural strength to support the higher running load (injury prevention).",
        "running_rule": (
            "**Dual-Sport Week (CRITICAL):** Running and cycling are both primary, and the week is built to "
            "IMPROVE both, not maintain them. The week holds 2-3 runs, 3-4 rides and 2-3 gym sessions - never more "
            "than 3 runs; `sessions_planned` has the exact mix for this week. How the week is built:\n"
            "    - **Hard days hard, easy days easy:** at most 3 quality sessions a week across both sports (bike "
            "quality, run quality, and in some Build weeks a second bike quality), never on consecutive days; every "
            "other session is truly easy (Zone 1-2, conversational). Never a hard run the day after a hard ride or a "
            "leg gym session (title names legs; other gym sessions don't count), or vice versa.\n"
            "    - **Runs:** one long easy run (60-90 min, at most ~30% of the week's running), one quality run in "
            "Build / race prep (tempo or threshold by heart rate, or hill repeats; in Base only strides or short hill "
            "sprints inside an easy run), the third run easy. Grow weekly run volume by at most ~10% a week. In a "
            "deload week: 2 easy runs, long run shortened.\n"
            "    - **Rides:** the bike quality session(s) the plan's focus names, one long Zone 2 ride (2-3 h, the "
            "main aerobic driver), the rest easy endurance rides.\n"
            "    - **Gym:** heavy leg strength (squat/deadlift/lunge pattern, 3-4 sets of 4-8 reps) goes on a hard "
            "day, after that day's quality session and ideally 6+ h later - keeping easy days easy and never "
            "landing the day before a quality session. The second gym session is upper body/core or light full "
            "body on an easy day; a third (Base weeks) is a short core/mobility session. Deload: 2 light sessions.\n"
            "    - **Typical week** (shift it around availability, keep the order of hard and easy): Mon rest or "
            "easy spin + upper/core gym; Tue bike quality + heavy legs gym later; Wed easy run with strides; Thu "
            "easy endurance ride; Fri run quality (Build) or easy run; Sat long ride; Sun long easy run. Long ride "
            "and long run back to back at the weekend train fatigue resistance.\n"
            "    - **Progression:** from one loading week to the next, add 10-15 min to the long ride or long run, or "
            "one rep / a few minutes to the main set of a quality session - one change at a time.\n"
            "    - **Week running short:** keep the bike quality, the run quality, the long ride and the long run; "
            "drop an easy ride first, then the third gym session."
        ),
        "short": "dual-sport: cycling and running are both primary goals this winter (2-3 runs, 3-4 rides, 2-3 gym a week)",
    },
}

# Dual-sport session mix (2-3 runs, 3-4 rides, 2-3 gym): deload weeks at the bottom of each range,
# loading weeks at the top. Bike quality sessions come from the cycling plan, at most 2, so with the
# quality run a week never holds more than 3 hard sessions.
DUAL_RIDES, DUAL_RIDES_DELOAD = 4, 3
DUAL_RUNS, DUAL_RUNS_DELOAD = 3, 2
DUAL_GYM_BASE, DUAL_GYM, DUAL_GYM_DELOAD = 3, 2, 2   # heavy strength is a Base emphasis, maintained in Build
MAX_BIKE_QUALITY = 2

RUN_FOCUS = {
    False: " Run: one quality run (tempo/threshold by HR or hill repeats), one long easy run, one easy run."
           " Gym: heavy legs on a hard day, upper body/core on an easy day.",
    True: " Run: 2 easy runs, long run shortened. Gym: 2 light sessions.",
}
# Early base (see core/periodization.py): no run intensity yet either.
RUN_FOCUS_BASE = (" Run: easy and conversational - one long easy run, strides or short hill sprints at most."
                  " Gym: 2 heavy strength sessions + 1 short core/mobility session.")


def get_focus() -> dict:
    """{'mode': 'cycling'|'dual', 'since': iso date or None, **MODES[mode]}."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM athlete_settings WHERE key = 'training_focus';")
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        row = None
    value = (row or {}).get("value") or {}
    mode = value.get("mode") if value.get("mode") in MODES else DEFAULT
    return {"mode": mode, "since": value.get("since"), **MODES[mode]}


def set_focus(mode: str) -> dict:
    if mode not in MODES:
        raise ValueError(f"Unknown training focus {mode!r}")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO athlete_settings (key, value, updated_at) VALUES ('training_focus', %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
            """, (json.dumps({"mode": mode, "since": date.today().isoformat()}),))
        conn.commit()
    finally:
        conn.close()
    return get_focus()


# Race and taper weeks (core/periodization.py) keep their own session mix in dual mode too.
FIXED_PHASES = ("Race", "Taper")


def adjust_sessions(sessions: dict | None, is_deload: bool, mode: str, phase: str | None = None) -> dict | None:
    """Dual mode: 2-3 runs, 3-4 rides, 2-3 gym. Keeps the cycling plan's bike quality (at most 2),
    fills the rest of the rides with endurance; deloads sit at the bottom of each range."""
    if not sessions or mode != "dual" or phase in FIXED_PHASES:
        return sessions
    if is_deload:
        return {"endurance": DUAL_RIDES_DELOAD, "sst": 0, "vo2": 0, "run": DUAL_RUNS_DELOAD, "weights": DUAL_GYM_DELOAD}
    vo2 = min(int(sessions.get("vo2", 0)), MAX_BIKE_QUALITY)
    sst = min(int(sessions.get("sst", 0)), MAX_BIKE_QUALITY - vo2)
    if phase == "Race prep" and int(sessions.get("run", 0)) < 2:
        # Before a BIKE A race the bike comes first: running drops to 2 (the long run + an easy
        # run keep the run base), the third run becomes a ride. Run race prep keeps its own runs.
        return {"endurance": DUAL_RIDES + 1 - sst - vo2, "sst": sst, "vo2": vo2, "run": DUAL_RUNS - 1,
                "weights": DUAL_GYM}
    return {"endurance": DUAL_RIDES - sst - vo2, "sst": sst, "vo2": vo2, "run": DUAL_RUNS,
            "weights": DUAL_GYM_BASE if phase == "Base" else DUAL_GYM}


def adjust_focus_text(text: str | None, is_deload: bool, mode: str, phase: str | None = None) -> str | None:
    if not text or mode != "dual" or phase in FIXED_PHASES:
        return text
    if text.startswith("Base - aerobic foundation"):
        return text + RUN_FOCUS_BASE
    return text + RUN_FOCUS[bool(is_deload)]
