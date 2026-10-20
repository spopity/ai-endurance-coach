"""
Deterministic calculators for athlete_twin's "what-if" scenarios, so projected
numbers come from formulas rather than from the LLM's mental arithmetic.

- project_training_load: forward-simulates CTL/ATL/TSB from today's
  Intervals.icu values using the same exponentially-weighted model Intervals.icu
  uses (CTL 42-day, ATL 7-day time constants).
- estimate_ride_time: steady-state cycling power model (gravity + rolling
  resistance + aero drag) solved for speed, for climbs, flats, or segments.
"""
import logging
import math
from datetime import date, timedelta
from typing import Literal, Optional

from core.db import get_connection
from core.metrics import load_athlete_profile

logger = logging.getLogger("AI_COACH_V1.simulators")

CTL_DAYS = 42
ATL_DAYS = 7
CTL_DECAY = math.exp(-1 / CTL_DAYS)
ATL_DECAY = math.exp(-1 / ATL_DAYS)
MAX_PROJECTION_WEEKS = 52


def _load_starting_point(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT day, ctl, atl FROM daily_metrics
            WHERE ctl IS NOT NULL AND atl IS NOT NULL
            ORDER BY day DESC LIMIT 1;
        """)
        row = cur.fetchone()
        cur.execute("""
            SELECT COALESCE(SUM(training_load), 0) AS load FROM activities
            WHERE superseded_by IS NULL AND start_time >= NOW() - INTERVAL '28 days';
        """)
        recent = cur.fetchone()
    return {"row": row, "avg_weekly_load_4w": round(float(recent["load"]) / 4, 1)}


def _training_day_offsets(days_per_week: int) -> set[int]:
    """Spreads N training days as evenly as possible across a 7-day week."""
    return {round(i * 7 / days_per_week) for i in range(days_per_week)}


def project_training_load(
    weekly_tss_plan: list[float],
    training_days_per_week: int = 5,
    report_date: Optional[str] = None,
) -> str:
    """Projects CTL (fitness), ATL (fatigue) and TSB (form) forward from the athlete's
    latest real values, given a planned training load per week.

    Args:
        weekly_tss_plan: planned TSS for each upcoming week, in order - e.g. [450, 450, 500, 300]
            for 3 build weeks and a recovery week, or [0] for a fully missed week. One entry per week.
        training_days_per_week: how many days per week the load is spread over (1-7); the rest are rest days.
        report_date: optional YYYY-MM-DD (e.g. a race day) to report the exact projected values for.
    """
    if not weekly_tss_plan:
        return "weekly_tss_plan must contain at least one week."
    if len(weekly_tss_plan) > MAX_PROJECTION_WEEKS:
        return f"Projection is limited to {MAX_PROJECTION_WEEKS} weeks."
    if any(t < 0 for t in weekly_tss_plan):
        return "Weekly TSS values cannot be negative."
    if not 1 <= training_days_per_week <= 7:
        return "training_days_per_week must be between 1 and 7."

    conn = get_connection()
    try:
        start = _load_starting_point(conn)
    except Exception as e:
        logger.error(f"Failed to load starting CTL/ATL: {e}", exc_info=True)
        return f"Database error: {e}"
    finally:
        conn.close()

    row = start["row"]
    if not row:
        return "No CTL/ATL history available in daily_metrics - cannot project."

    ctl, atl = float(row["ctl"]), float(row["atl"])
    day = row["day"]
    target = None
    if report_date:
        try:
            target = date.fromisoformat(report_date)
        except ValueError:
            return f"report_date '{report_date}' is not a valid YYYY-MM-DD date."

    lines = [
        f"Starting point ({day}, from Intervals.icu): CTL {ctl:.1f}, ATL {atl:.1f}, TSB {ctl - atl:+.1f}",
        f"Actual load over the last 4 weeks: avg {start['avg_weekly_load_4w']} TSS/week",
        f"Projection (each week's TSS spread over {training_days_per_week} evenly spaced training days; "
        f"exponential model, CTL {CTL_DAYS}d / ATL {ATL_DAYS}d - same as Intervals.icu):",
    ]

    offsets = _training_day_offsets(training_days_per_week)
    peak_ctl, min_tsb, max_tsb = ctl, ctl - atl, ctl - atl
    target_values = None

    for week_idx, weekly_tss in enumerate(weekly_tss_plan, start=1):
        daily_tss = weekly_tss / training_days_per_week
        for d in range(7):
            day += timedelta(days=1)
            tss = daily_tss if d in offsets else 0.0
            ctl = ctl * CTL_DECAY + tss * (1 - CTL_DECAY)
            atl = atl * ATL_DECAY + tss * (1 - ATL_DECAY)
            tsb = ctl - atl
            peak_ctl, min_tsb, max_tsb = max(peak_ctl, ctl), min(min_tsb, tsb), max(max_tsb, tsb)
            if target and day == target:
                target_values = (ctl, atl, tsb)
        lines.append(
            f"- Week {week_idx} (to {day}): {weekly_tss:g} TSS -> CTL {ctl:.1f}, ATL {atl:.1f}, TSB {ctl - atl:+.1f}"
        )

    lines.append(
        f"End of projection ({day}): CTL {ctl:.1f}, ATL {atl:.1f}, TSB {ctl - atl:+.1f}. "
        f"Peak CTL {peak_ctl:.1f}; TSB range {min_tsb:+.1f} to {max_tsb:+.1f}."
    )
    if target:
        if target_values:
            c, a, t = target_values
            lines.append(f"On {target}: CTL {c:.1f}, ATL {a:.1f}, TSB {t:+.1f}.")
        else:
            lines.append(f"{target} is outside the projected range ({row['day'] + timedelta(days=1)} to {day}).")
    lines.append(
        "Rule of thumb: TSB below about -30 means high overreaching risk; +5 to +25 is a typical fresh race-day range."
    )
    return "\n".join(lines)


# --- Ride / climb time ---

G = 9.81
DRIVETRAIN_EFFICIENCY = 0.97

# (CdA m², Crr) per riding position/surface - typical published values.
POSITIONS = {
    "hoods": (0.36, 0.004),
    "drops": (0.31, 0.004),
    "tt": (0.24, 0.004),
    "gravel": (0.38, 0.008),
    "mtb": (0.45, 0.012),
}


def _power_required(v: float, mass: float, slope: float, cda: float, crr: float, rho: float) -> float:
    theta = math.atan(slope)
    return v * mass * G * (math.sin(theta) + crr * math.cos(theta)) + 0.5 * rho * cda * v ** 3


def _solve_speed(power_w: float, mass: float, slope: float, cda: float, crr: float, rho: float) -> float:
    """Bisection for the steady speed at which required power equals delivered power."""
    lo, hi = 0.05, 40.0
    if _power_required(hi, mass, slope, cda, crr, rho) < power_w:
        return hi
    for _ in range(100):
        mid = (lo + hi) / 2
        if _power_required(mid, mass, slope, cda, crr, rho) < power_w:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def steady_speed_kmh(
    power_w: float,
    total_mass_kg: float,
    gradient_pct: float,
    position: str = "hoods",
    altitude_m: float = 0.0,
    cda_factor: float = 1.0,
) -> float:
    """Public building block for other planners (e.g. race pacing): steady speed in km/h
    for a given power on a given gradient. cda_factor < 1 models drafting in a group."""
    cda, crr = POSITIONS.get(position, POSITIONS["hoods"])
    rho = 1.225 * math.exp(-altitude_m / 8500)
    v = _solve_speed(power_w * DRIVETRAIN_EFFICIENCY, total_mass_kg, gradient_pct / 100, cda * cda_factor, crr, rho)
    return v * 3.6


def _format_hms(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def estimate_ride_time(
    distance_km: float,
    avg_gradient_pct: float = 0.0,
    power_w: Optional[float] = None,
    pct_ftp: Optional[float] = None,
    rider_weight_kg: Optional[float] = None,
    bike_weight_kg: float = 8.5,
    position: Literal["hoods", "drops", "tt", "gravel", "mtb"] = "hoods",
    altitude_m: float = 0.0,
) -> str:
    """Estimates time and speed for a ride segment or climb at a steady power, using a physics
    model (gravity + rolling resistance + aerodynamic drag). Use it for climb times (e.g. Alpe d'Huez:
    13.8 km at 8.1%), flat time-trial speeds, or "what if I weighed X kg / had Y watts" comparisons -
    call it once per scenario to compare.

    Args:
        distance_km: segment length in km.
        avg_gradient_pct: average gradient in percent (0 for flat, negative for descents).
        power_w: steady power in watts. Give either this or pct_ftp; if neither, 85% of FTP is used.
        pct_ftp: steady power as a percentage of the athlete's current FTP (e.g. 90).
        rider_weight_kg: rider weight; defaults to the athlete's current weight.
        bike_weight_kg: bike + gear weight (default 8.5 kg).
        position: riding position/surface - hoods, drops, tt, gravel or mtb.
        altitude_m: average altitude of the segment in metres (thinner air means less drag).
    """
    if distance_km <= 0:
        return "distance_km must be positive."

    profile = {}
    if rider_weight_kg is None or power_w is None:
        conn = get_connection()
        try:
            profile = load_athlete_profile(conn)
        except Exception as e:
            logger.error(f"Failed to load athlete profile: {e}", exc_info=True)
        finally:
            conn.close()

    ftp = profile.get("ftp") or 250
    rider = rider_weight_kg or float(profile.get("weight_kg") or 75)
    if power_w is None:
        power_w = ftp * (pct_ftp if pct_ftp is not None else 85) / 100
    if power_w <= 0:
        return "Power must be positive."
    if position not in POSITIONS:
        return f"position must be one of {', '.join(POSITIONS)}."

    cda, crr = POSITIONS[position]
    rho = 1.225 * math.exp(-altitude_m / 8500)
    mass = rider + bike_weight_kg
    slope = avg_gradient_pct / 100

    v = _solve_speed(power_w * DRIVETRAIN_EFFICIENCY, mass, slope, cda, crr, rho)
    seconds = distance_km * 1000 / v
    lines = [
        f"Segment: {distance_km:g} km at {avg_gradient_pct:+g}% ({position}, altitude {altitude_m:g} m)",
        f"Power: {power_w:.0f} W ({power_w / ftp * 100:.0f}% of FTP {ftp} W), {power_w / rider:.2f} W/kg "
        f"at {rider:g} kg rider + {bike_weight_kg:g} kg bike",
        f"Estimated time: {_format_hms(seconds)} at {v * 3.6:.1f} km/h average",
    ]
    if avg_gradient_pct > 0:
        gain = distance_km * 1000 * slope
        lines.append(f"Elevation gain: ~{gain:.0f} m, VAM ~{gain / (seconds / 3600):.0f} m/h")
    if v >= 39.9:
        lines.append("Note: speed hit the model's 144 km/h cap - treat as unrealistic (descent).")
    lines.append(
        f"Assumptions: steady power, no wind, CdA {cda} m², Crr {crr}, drivetrain efficiency "
        f"{DRIVETRAIN_EFFICIENCY:.0%}. Real-world times vary with pacing, wind, surface and gradient changes."
    )
    return "\n".join(lines)
