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
"""

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
