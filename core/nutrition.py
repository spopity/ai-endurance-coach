"""
Fueling for one workout: a short line (before / during / after) at the top of the workout
description, so it shows on Intervals.icu, Garmin, Telegram and the dashboard next to the steps.
Numbers only, from the session's duration, intensity, sport and the athlete's weight:

- During (carbs per hour; current sports-nutrition consensus, Jeukendrup / ACSM-IOC, and the
  higher intakes pros train their gut for): under 60 min nothing but water; 60-90 min 30 g/h
  (easy sessions under 75 min need none); 90-150 min 60 g/h, 75 g/h when hard; over 150 min
  90 g/h on the bike, 75 g/h running (the gut tolerates less while running). Fluids with
  electrolytes from 90 min.
- Before: hard or long (90+ min) sessions start fuelled - ~1.5 g/kg carbs 2-3 h before;
  otherwise a normal meal.
- After: ~0.3 g/kg protein (0.4 g/kg after gym); after hard or long sessions also ~1 g/kg carbs
  within the hour to refill glycogen for the next session.

The whole day (daily_carbs, shown per day of the 7-day plan): carbs follow the day's training
("fuel for the work required", ACSM/IOC 2016, Burke 2011, Impey 2018), protein stays steady:
- rest day or gym only: 3-4 g/kg (gym 4-5),
- easy, under 60 min: 4-5 g/kg,
- about an hour, or a hard session: 5-7 g/kg,
- 90-150 min, or hard and over 75 min: 6-8 g/kg,
- over 150 min: 8-10 g/kg,
- the day before a long or hard session: the top of the range (+1 g/kg), so it starts with full
  glycogen; a low-energy flag (core/energy.py) also moves the day to the top of its range,
- protein 1.6-2.0 g/kg every day, spread over 4 meals.
"""
import re

DEFAULT_WEIGHT_KG = 72
PREFIX = "Fuel: "
FLUIDS_FROM_MIN = 90


def _r5(x: float) -> int:
    return int(5 * round(x / 5))


def carbs_per_hour(sport: str, minutes: int | None, hard: bool) -> int:
    if sport == "gym" or not minutes or minutes < 60:
        return 0
    if minutes < 90:
        return 30 if hard or minutes >= 75 else 0
    if minutes <= 150:
        return 75 if hard else 60
    return 90 if sport == "ride" else 75


def fuel(sport: str, minutes: int | None, hard: bool, weight_kg: float | None = None) -> dict:
    """{before, during, after, text} for one session."""
    w = float(weight_kg or DEFAULT_WEIGHT_KG)
    big = bool(minutes and minutes >= FLUIDS_FROM_MIN)
    demanding = sport != "gym" and (hard or big)
    before = f"~{_r5(1.5 * w)} g carbs 2-3 h before" if demanding else "normal meal before"
    cph = carbs_per_hour(sport, minutes, hard)
    if cph:
        total = _r5(cph * minutes / 60)
        during = f"{cph} g carbs/h (~{total} g)" + (" + 750-1000 ml/h with electrolytes" if big else "")
    else:
        during = "water only" if sport != "gym" else ""
    protein = _r5((0.4 if sport == "gym" else 0.3) * w)
    after = f"{protein} g protein" + (f" + {_r5(w)} g carbs within 1 h" if demanding else "")
    parts = [before] + ([f"during {during}"] if during else []) + [f"after {after}"]
    return {"before": before, "during": during, "after": after, "carbs_per_hour": cph,
            "text": PREFIX + " · ".join(parts)}


def without_fuel(description: str | None) -> str:
    """A workout description minus its fueling line - for places that only want the session
    (the public Strava note: the grams come from body weight, and nobody knows what was eaten)."""
    return "\n".join(l for l in (description or "").split("\n") if not l.startswith(PREFIX)).strip()


# --- the whole day ---

PROTEIN_G_PER_KG = (1.6, 2.0)
SESSION_MINUTES = {"gym_legs": 45, "gym_upper": 35, "gym_core": 25}
_ABOUT = re.compile(r"~\s*(\d+)\s*min")
_RANGE = re.compile(r"(\d+)\s*-\s*(\d+)\s*min")
_PLAIN = re.compile(r"(\d+)\s*min")


def session_minutes(detail: str | None, kind: str = "", max_minutes: int | None = None,
                    default: int = 45) -> int:
    """Planned minutes of a session from its text ("(~45 min)", "60-75 min", "30 min")."""
    if kind in SESSION_MINUTES:
        return SESSION_MINUTES[kind]
    t = detail or ""
    if m := _ABOUT.search(t):
        mins = int(m.group(1))
    elif m := _RANGE.search(t):
        mins = (int(m.group(1)) + int(m.group(2))) // 2
    elif m := _PLAIN.search(t):
        mins = int(m.group(1))
    else:
        mins = default
    return min(mins, max_minutes) if max_minutes else mins


def daily_carbs(sessions: list[dict], weight_kg: float | None = None, next_day: list[dict] | None = None,
                low_energy: bool = False) -> dict:
    """Carbs for a whole day from its sessions [{sport, minutes, hard}]. `next_day`: tomorrow's
    sessions (a long or hard one lifts today to the top of its range).
    -> {g_per_kg: (lo, hi), grams: (lo, hi), protein: (lo, hi), why, text}."""
    w = float(weight_kg or DEFAULT_WEIGHT_KG)
    endurance = [s for s in sessions if s["sport"] != "gym"]
    minutes = sum(s["minutes"] for s in endurance)
    hard = any(s["hard"] for s in endurance)
    if not endurance:
        lo, hi, why = (4, 5, "gym day") if sessions else (3, 4, "rest day")
    elif minutes > 150:
        lo, hi, why = 8, 10, f"{minutes} min of training"
    elif minutes >= 90 or (hard and minutes > 75):
        lo, hi, why = 6, 8, f"{minutes} min" + (", hard" if hard else "")
    elif minutes >= 60 or hard:
        lo, hi, why = 5, 7, "hard session" if hard else f"{minutes} min"
    else:
        lo, hi, why = 4, 5, f"easy, {minutes} min"
    big_tomorrow = any(s["sport"] != "gym" and (s["hard"] or s["minutes"] >= 90) for s in next_day or [])
    if big_tomorrow:
        lo, hi, why = lo + 1, hi + 1, why + "; fuel up for tomorrow"
    if low_energy:
        lo, why = hi - 0.5, why + "; top of the range (low-energy flag)"
    g = (_r5(lo * w), _r5(hi * w))
    p = (_r5(PROTEIN_G_PER_KG[0] * w), _r5(PROTEIN_G_PER_KG[1] * w))
    return {"g_per_kg": (lo, hi), "grams": g, "protein": p, "why": why,
            "text": f"Carbs {g[0]}-{g[1]} g ({lo:g}-{hi:g} g/kg, {why}) · protein {p[0]}-{p[1]} g"}
