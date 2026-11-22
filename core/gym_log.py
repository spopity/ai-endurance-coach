"""
Gym lifts, logged by the athlete in Telegram (`/gym squat 80x5x3, rdl 60x8x3, pull-up bwx8x3`)
into gym_sets (migration 017), and the progression the plan prescribes from them.

Format per exercise: `<name> <weight>x<reps>x<sets>` - weight in kg or `bw` (bodyweight), sets
optional (1). Exercises are separated by commas, semicolons or new lines. Names are normalised
through ALIASES ("back squat" and "squats" are both "squat").

Progression (simple double progression, the standard for strength work alongside endurance):
- the last session of that exercise (6 weeks back at most) felt RPE <= 8 (or wasn't rated): add
  2.5 kg to lower-body lifts, 1 kg to upper-body ones, same reps and sets,
- RPE 9-10: repeat the weight,
- bodyweight: one more rep per set.
Numbers come from what was logged, never from the model.
"""
import logging
import re
from datetime import date, timedelta

from core.overview import query

logger = logging.getLogger("AI_COACH_V1.gym_log")

ALIASES = {
    "squat": ["squat", "squats", "back squat", "back squats", "bs"],
    "front squat": ["front squat", "front squats"],
    "deadlift": ["deadlift", "deadlifts", "dl"],
    "trap bar deadlift": ["trap bar deadlift", "trap bar", "trap-bar deadlift", "hex bar", "tbdl"],
    "romanian deadlift": ["romanian deadlift", "rdl", "rdls", "romanian"],
    "split squat": ["split squat", "split squats", "bulgarian split squat", "bulgarian", "bss"],
    "lunge": ["lunge", "lunges", "walking lunge", "walking lunges"],
    "calf raise": ["calf raise", "calf raises", "calves"],
    "hip thrust": ["hip thrust", "hip thrusts"],
    "leg press": ["leg press"],
    "step-up": ["step-up", "step up", "step-ups", "step ups"],
    "bench press": ["bench press", "bench", "bp"],
    "overhead press": ["overhead press", "ohp", "military press", "shoulder press"],
    "row": ["row", "rows", "barbell row", "bent over row", "dumbbell row", "db row"],
    "pull-up": ["pull-up", "pull up", "pullup", "pullups", "pull-ups", "chin-up", "chin up", "chinup", "chinups"],
    "push-up": ["push-up", "push up", "pushup", "pushups", "push-ups"],
    "dip": ["dip", "dips"],
}
LOOKUP = {a: name for name, aliases in ALIASES.items() for a in aliases}
LOWER = {"squat", "front squat", "deadlift", "trap bar deadlift", "romanian deadlift", "split squat", "lunge",
         "calf raise", "hip thrust", "leg press", "step-up"}
LOWER_STEP_KG, UPPER_STEP_KG = 2.5, 1.0
LOOKBACK_WEEKS = 6
ENTRY = re.compile(r"^\s*([a-z][a-z \-']*?)\s+(bw|\d+(?:[.,]\d+)?)\s*(?:kg)?\s*[x×*]\s*(\d+)(?:\s*[x×*]\s*(\d+))?\s*$",
                   re.IGNORECASE)


def normalise(name: str) -> str:
    n = re.sub(r"\s+", " ", name.strip().lower())
    return LOOKUP.get(n, n)


def parse(text: str) -> tuple[list[dict], list[str]]:
    """(entries, unreadable parts)."""
    items, bad = [], []
    for part in filter(None, (p.strip() for p in re.split(r"[;\n]|,(?!\d)", text))):  # "52,5" is a number
        m = ENTRY.match(part)
        if not m:
            bad.append(part)
            continue
        name, weight, reps, sets = m.groups()
        items.append({"exercise": normalise(name),
                      "weight_kg": None if weight.lower() == "bw" else float(weight.replace(",", ".")),
                      "reps": int(reps), "sets": int(sets or 1)})
    return items, bad


def save(day: date, items: list[dict]) -> str | None:
    """Stores the lifts for `day` (linked to that day's gym activity if there is one); returns its id."""
    from core.gym import WEIGHTS_TYPES
    rows = query("""SELECT external_id FROM activities WHERE superseded_by IS NULL AND type = ANY(%s)
                    AND (start_time AT TIME ZONE 'Europe/Bratislava')::date = %s ORDER BY start_time DESC LIMIT 1;""",
                 (list(WEIGHTS_TYPES), day))
    eid = rows[0]["external_id"] if rows else None
    from core.db import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for i in items:
                cur.execute("INSERT INTO gym_sets (day, external_id, exercise, weight_kg, reps, sets) "
                            "VALUES (%s, %s, %s, %s, %s, %s);",
                            (day, eid, i["exercise"], i["weight_kg"], i["reps"], i["sets"]))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Logged {len(items)} lifts for {day}.")
    return eid


def fmt(e: dict) -> str:
    w = "bodyweight" if e["weight_kg"] is None else f"{float(e['weight_kg']):g} kg"
    return f"{e['exercise']} {w} {e['sets']}x{e['reps']}"


def next_step(e: dict, rpe: int | None) -> str:
    """The progression for one exercise from its last session."""
    if e["weight_kg"] is None:
        return f"{e['exercise']} bodyweight {e['sets']}x{e['reps'] + 1}"
    if rpe is not None and rpe >= 9:
        return f"{e['exercise']} {float(e['weight_kg']):g} kg {e['sets']}x{e['reps']} (repeat - last time was RPE {rpe})"
    step = LOWER_STEP_KG if e["exercise"] in LOWER else UPPER_STEP_KG
    return f"{e['exercise']} {float(e['weight_kg']) + step:g} kg {e['sets']}x{e['reps']}"


def last_lifts(lower: bool, today: date | None = None) -> list[dict]:
    """The latest logged set per exercise (lower or upper body) of the last LOOKBACK_WEEKS, with
    that session's RPE."""
    today = today or date.today()
    rows = query("""
        SELECT DISTINCT ON (g.exercise) g.exercise, g.weight_kg, g.reps, g.sets, g.day,
               COALESCE(f.rpe, (a.raw_payload->>'icu_rpe')::int) AS rpe
        FROM gym_sets g
        LEFT JOIN session_feedback f ON f.external_id = g.external_id
        LEFT JOIN activities a ON a.external_id = g.external_id AND a.superseded_by IS NULL
        WHERE g.day >= %s ORDER BY g.exercise, g.day DESC, g.weight_kg DESC NULLS LAST;
    """, (today - timedelta(weeks=LOOKBACK_WEEKS),))
    return [r for r in rows if (r["exercise"] in LOWER) == lower]


def prescription(lower: bool, today: date | None = None) -> str | None:
    """'Last time ...; today ...' for the plan, or None with nothing logged."""
    lifts = last_lifts(lower, today)
    if not lifts:
        return None
    return "Progression from your log: " + "; ".join(next_step(e, e["rpe"]) for e in lifts)


def recent(days: int = 14) -> list[dict]:
    rows = query("SELECT day, exercise, weight_kg, reps, sets FROM gym_sets WHERE day >= %s ORDER BY day DESC, id;",
                 (date.today() - timedelta(days=days),))
    return [{"date": r["day"].isoformat(), "lift": fmt(r)} for r in rows]
