"""
Deterministic race planning, built on the numbers real coaches use:

- Course model: distance + total elevation split into climb / flat / descent
  (no GPX yet - uses typical climb gradients per surface).
- Pacing: target intensity factor (IF = NP/FTP) by expected duration
  (Coggan/Allen race-IF guidelines), climb/flat/descent power targets, HR caps
  as % of LTHR, RPE - then physics (core/simulators.py) for a time prediction,
  iterated until intensity and duration agree. Heat lowers targets.
- Fuelling: carbs/h by duration, fluid + sodium for a heavy sweater adjusted to
  temperature, caffeine dosing in mg/kg for a habitual high user, carb loading.
- Taper: length by priority (A/B/C) and fitness, CTL/ATL/TSB projected to race
  morning with the Intervals.icu model, compared with target form ranges.
- Race week: day-by-day template; past races from the athlete's own history as
  reference; post-race review against the same model.
"""
import logging
import math
from datetime import date, timedelta

from core.db import get_connection, load_env
from core.metrics import load_athlete_profile
from core.races import fetch_upcoming_races, format_past_races, past_races
from core.simulators import ATL_DECAY, CTL_DECAY, steady_speed_kmh
from core.weather import race_weather

logger = logging.getLogger("AI_COACH_V1.race_planner")

DEFAULT_LOCATION = "Bratislava, Slovakia"

# Per race type: riding position (CdA/Crr in simulators), drafting on flats (CdA multiplier
# for a mass start), descent speed cap, typical climb gradient, technical slow-down, bike
# mass, elevation assumption (m/km) when none is given, and time-prediction uncertainty.
SURFACES = {
    "Ride":             dict(label="road",   position="hoods",  draft=0.75, descent_cap=60, climb_grad=5.0, tech=1.00, bike=8.5,  m_per_km=8,  spread=0.07),
    "VirtualRide":      dict(label="indoor", position="hoods",  draft=1.00, descent_cap=60, climb_grad=5.0, tech=1.00, bike=8.5,  m_per_km=8,  spread=0.05),
    "GravelRide":       dict(label="gravel", position="gravel", draft=0.85, descent_cap=45, climb_grad=5.5, tech=0.95, bike=9.5,  m_per_km=10, spread=0.10),
    "MountainBikeRide": dict(label="MTB",    position="mtb",    draft=1.00, descent_cap=30, climb_grad=7.0, tech=0.85, bike=12.0, m_per_km=22, spread=0.15),
}
RUN_TYPES = {"Run", "TrailRun"}

# Plain-language race types (as an LLM or a calendar description may give them) -> Intervals.icu types.
TYPE_ALIASES = {
    "road": "Ride", "ride": "Ride", "crit": "Ride", "criterium": "Ride", "tt": "Ride", "time trial": "Ride",
    "gravel": "GravelRide", "gravelride": "GravelRide", "cx": "GravelRide", "cyclocross": "GravelRide",
    "mtb": "MountainBikeRide", "mountain bike": "MountainBikeRide", "mountainbikeride": "MountainBikeRide",
    "xco": "MountainBikeRide", "xcm": "MountainBikeRide",
    "indoor": "VirtualRide", "virtual": "VirtualRide", "zwift": "VirtualRide", "virtualride": "VirtualRide",
    "run": "Run", "trail": "TrailRun", "trail run": "TrailRun", "trailrun": "TrailRun",
}


def normalize_race_type(value: str | None) -> str:
    if not value:
        return "Ride"
    if value in SURFACES or value in RUN_TYPES:
        return value
    return TYPE_ALIASES.get(value.strip().lower(), value)

# Race intensity factor (NP/FTP) a well-paced athlete can hold, by duration in hours.
IF_BY_HOURS = [(0.33, 1.05), (0.5, 1.00), (1, 0.95), (1.5, 0.90), (2, 0.86), (3, 0.81), (4, 0.77),
               (5, 0.74), (6, 0.71), (8, 0.67), (10, 0.63), (14, 0.58), (24, 0.52)]
# Average HR as a share of LTHR, by duration.
HR_BY_HOURS = [(0.5, (0.98, 1.05)), (1, (0.95, 1.02)), (2, (0.90, 0.96)), (4, (0.84, 0.91)),
               (6, (0.80, 0.87)), (10, (0.75, 0.83)), (24, (0.68, 0.78))]

TARGET_TSB = {"A": (10, 25), "B": (5, 15), "C": (-10, 5)}


def _interp(table, x):
    if x <= table[0][0]:
        return table[0][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x <= x1:
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return table[-1][1]


def _hr_band(hours):
    for h, band in HR_BY_HOURS:
        if hours <= h:
            return band
    return HR_BY_HOURS[-1][1]


def _hms(hours: float) -> str:
    total = int(round(hours * 3600))
    h, rem = divmod(total, 3600)
    return f"{h}h{rem // 60:02d}"


# --- athlete state ---

def _athlete_state() -> dict:
    conn = get_connection()
    try:
        profile = load_athlete_profile(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT day, ctl, atl FROM daily_metrics WHERE ctl IS NOT NULL ORDER BY day DESC LIMIT 1;")
            load = cur.fetchone()
            cur.execute("""
                SELECT COALESCE(SUM(training_load), 0) AS load FROM activities
                WHERE superseded_by IS NULL AND start_time >= NOW() - INTERVAL '28 days';
            """)
            weekly = float(cur.fetchone()["load"]) / 4
            cur.execute("""
                SELECT distance_m, moving_time_sec, start_time FROM activities
                WHERE type IN ('Run', 'TrailRun') AND superseded_by IS NULL AND distance_m >= 5000
                  AND start_time >= NOW() - INTERVAL '120 days'
                ORDER BY distance_m / NULLIF(moving_time_sec, 0) DESC LIMIT 1;
            """)
            best_run = cur.fetchone()
            cur.execute("""
                SELECT type FROM activities
                WHERE avg_power_w IS NOT NULL AND superseded_by IS NULL AND start_time >= NOW() - INTERVAL '365 days'
                GROUP BY type HAVING COUNT(*) >= 3;
            """)
            power_types = {r["type"] for r in cur.fetchall()}
    finally:
        conn.close()
    return {
        "ftp": float(profile.get("ftp") or 250),
        "lthr": float(profile.get("fthr") or 165),
        "weight": float(profile.get("weight_kg") or 75),
        "eftp": float(profile["eftp"]) if profile.get("eftp") else None,
        "w_prime": float(profile["w_prime_j"]) if profile.get("w_prime_j") else None,
        "ctl": float(load["ctl"]) if load else None,
        "atl": float(load["atl"]) if load else None,
        "load_day": load["day"] if load else None,
        "weekly_load": weekly,
        "best_run": dict(best_run) if best_run else None,
        "power_types": power_types,
    }


# --- course + pacing (cycling) ---

def _cycling_pacing(distance_km, elevation_m, race_type, athlete, temp_max, altitude_m):
    s = SURFACES.get(race_type, SURFACES["Ride"])
    ftp, mass = athlete["ftp"], athlete["weight"] + s["bike"]

    climb_km = elevation_m / (s["climb_grad"] * 10) if elevation_m else 0
    grad = s["climb_grad"]
    if climb_km > 0.4 * distance_km:  # very hilly: steeper climbs rather than more of them
        climb_km = 0.4 * distance_km
        grad = elevation_m / (climb_km * 10)
    descent_km = climb_km
    flat_km = max(distance_km - climb_km - descent_km, 0)

    heat_cut = min(0.08, max(0.0, (temp_max or 20) - 25) * 0.01)
    hours = distance_km / 25
    for _ in range(6):
        target_if = _interp(IF_BY_HOURS, hours) * (1 - heat_cut)
        target = target_if * ftp
        climb_cap = 1.0 if hours < 1.5 else 0.95 if hours < 3 else 0.90
        climb_p = min(target * 1.08, climb_cap * ftp)
        flat_p = target * 0.95
        descent_p = 0.35 * ftp
        v_climb = steady_speed_kmh(climb_p, mass, grad, s["position"], altitude_m) * s["tech"]
        v_flat = steady_speed_kmh(flat_p, mass, 0, s["position"], altitude_m, s["draft"]) * s["tech"]
        v_desc = min(steady_speed_kmh(descent_p, mass, -grad, s["position"], altitude_m), s["descent_cap"]) * s["tech"]
        t_climb, t_flat, t_desc = climb_km / v_climb, flat_km / v_flat, descent_km / v_desc
        new_hours = t_climb + t_flat + t_desc
        if abs(new_hours - hours) < 0.01:
            hours = new_hours
            break
        hours = new_hours

    # NP over the race (4th-power mean of segment powers, weighted by time).
    np_w = (sum(p ** 4 * t for p, t in ((climb_p, t_climb), (flat_p, t_flat), (descent_p, t_desc))) / hours) ** 0.25
    avg_w = (climb_p * t_climb + flat_p * t_flat + descent_p * t_desc) / hours
    return {
        "surface": s, "hours": hours, "spread": s["spread"], "target_if": target_if, "heat_cut": heat_cut,
        "climb_km": climb_km, "climb_grad": grad, "flat_km": flat_km, "descent_km": descent_km,
        "climb_p": climb_p, "flat_p": flat_p, "descent_p": descent_p, "np": np_w, "avg_w": avg_w,
        "kj": avg_w * hours * 3.6, "speed": distance_km / hours,
        "t_climb": t_climb, "t_flat": t_flat, "t_desc": t_desc,
    }


def _run_prediction(distance_km, elevation_m, athlete):
    best = athlete.get("best_run")
    if not best or not best.get("moving_time_sec"):
        return None
    d1 = best["distance_m"] / 1000
    t1 = best["moving_time_sec"] / 3600
    effort_km = distance_km + (elevation_m or 0) / 100  # ~100 m of climbing costs about 1 flat km
    hours = t1 * (effort_km / d1) ** 1.06  # Riegel
    return {"hours": hours, "reference": f"{d1:.1f} km in {_hms(t1)} on {best['start_time']:%Y-%m-%d}",
            "effort_km": effort_km}


# --- fuelling ---

def _fuelling(hours, temp_max, weight, is_run):
    temp = temp_max if temp_max is not None else 20
    if hours < 1:
        carbs = (0, 30)
    elif hours < 2:
        carbs = (40, 60)
    elif hours < 3:
        carbs = (60, 90)
    else:
        carbs = (80, 90) if not is_run else (60, 80)
    if temp < 15:
        fluid, sodium = (600, 800), (600, 900)
    elif temp < 22:
        fluid, sodium = (750, 1000), (800, 1100)
    elif temp < 28:
        fluid, sodium = (900, 1200), (900, 1300)
    else:
        fluid, sodium = (1000, 1300), (1000, 1500)
    caffeine_pre = round(3 * weight / 10) * 10
    caffeine_max = round(6 * weight / 10) * 10
    return {
        "carbs_h": carbs, "carbs_total": (round(carbs[0] * hours), round(carbs[1] * hours)),
        "fluid_h": fluid, "fluid_total_l": (fluid[0] * hours / 1000, fluid[1] * hours / 1000),
        "sodium_h": sodium, "temp": temp,
        "bottles": math.ceil(fluid[1] * hours / 750) if hours >= 1 else 1,
        "caffeine_pre": caffeine_pre, "caffeine_top_up": (round(weight / 10) * 10, round(2 * weight / 10) * 10),
        "caffeine_max": caffeine_max,
        "carb_load": (round(8 * weight), round(10 * weight)) if hours >= 1.5 else None,
        "breakfast": (round(2 * weight), round(3 * weight)),
    }


# --- taper / form projection ---

def _simulate(ctl, atl, daily_loads):
    for load in daily_loads:
        ctl = ctl * CTL_DECAY + load * (1 - CTL_DECAY)
        atl = atl * ATL_DECAY + load * (1 - ATL_DECAY)
    return ctl, atl


def _taper(priority, athlete, race_day):
    if athlete["ctl"] is None or race_day is None:
        return None
    start_day = athlete["load_day"] + timedelta(days=1)
    days = (race_day - start_day).days  # days of training left before race morning
    if days < 0:
        return None
    ctl0 = athlete["ctl"]
    taper_days = {"A": 7 if ctl0 < 40 else 10 if ctl0 < 70 else 14, "B": 4, "C": 1}.get(priority, 7)
    taper_days = min(taper_days, days)
    base = athlete["weekly_load"] / 7

    def scenario(weekly_growth=0.0, taper_factor=0.5, extra_taper=0):
        t_days = min(days, taper_days + extra_taper)
        loads, daily = [], base
        for i in range(days - t_days):
            if i and i % 7 == 0:
                daily *= 1 + weekly_growth
            loads.append(daily)
        pre_taper = daily
        for i in range(t_days):
            loads.append(pre_taper * (0.3 if i == t_days - 1 else taper_factor))
        ctl, atl = _simulate(ctl0, athlete["atl"], loads)
        return {"ctl": ctl, "atl": atl, "tsb": ctl - atl, "taper_days": t_days,
                "pre_taper_weekly": pre_taper * 7}

    result = {"days": days, "taper_days": taper_days, "target": TARGET_TSB.get(priority, (5, 20)),
              "maintain": scenario()}
    if days - taper_days >= 21:
        result["build"] = scenario(weekly_growth=0.07)
    low = result["target"][0]
    if result["maintain"]["tsb"] < low and priority in ("A", "B"):
        result["deeper"] = scenario(taper_factor=0.4, extra_taper=3)
    return result


def _race_week(priority, pacing):
    if priority == "C":
        return ["Train through - just make the day before easy (≤45 min Z1-Z2) and skip intensity 2 days out."]
    race_p = f"{pacing['flat_p']:.0f} W" if pacing else "race effort"
    week = [
        f"D-7: normal endurance ride 90-120 min with 3x8 min at race pace ({race_p}).",
        "D-6: easy 60 min Z1-Z2 or rest.",
        "D-5: 60-75 min with 4x3 min VO2 (~115% FTP), full recoveries - keeps the top end sharp.",
        "D-4: easy 45-60 min Z2.",
        "D-3: 60 min with 3x5 min at race pace. Start carb loading if the race is >90 min.",
        "D-2: rest or 30 min very easy spin. Prepare bike, kit, bottles and gels.",
        f"D-1: openers - 45 min with 3x1 min at race pace ({race_p}) + 2-3 short sprints. Sleep is the priority.",
    ]
    return week if priority == "A" else week[3:]


# --- public API ---

def _resolve_race(race_id, **manual):
    race = {}
    if race_id:
        for r in fetch_upcoming_races():
            if str(r["id"]) == str(race_id) or r["name"].lower() == str(race_id).lower():
                race = r
                break
        else:
            raise ValueError(f"No upcoming race '{race_id}' in the Intervals.icu calendar.")
    for key, value in manual.items():
        if value not in (None, ""):
            race[key] = value
    return race


def compute_race_plan(race_id=None, **manual) -> dict:
    race = _resolve_race(race_id, **manual)
    if not race.get("distance_km"):
        raise ValueError("Race distance is required (add 'Distance: X km' to the Intervals.icu event or give it in chat).")
    race_type = normalize_race_type(race.get("type"))
    priority = (race.get("priority") or "A").upper()[:1]
    distance = float(race["distance_km"])
    assumptions = []

    if race_type not in RUN_TYPES and race_type not in SURFACES:
        assumptions.append(f"Race type '{race_type}' treated as a road race.")
        race_type = "Ride"
    if race.get("elevation_m") is None:
        per_km = SURFACES.get(race_type, {}).get("m_per_km", 10)
        race["elevation_m"] = distance * per_km
        assumptions.append(f"No elevation given - assumed {race['elevation_m']:.0f} m (typical {per_km} m/km); "
                           "add the real figure for a better prediction.")
    elevation = float(race["elevation_m"])

    race_day = date.fromisoformat(race["date"]) if race.get("date") else None
    location = race.get("location") or load_env().get("HOME_LOCATION") or DEFAULT_LOCATION
    if not race.get("location"):
        assumptions.append(f"No location given - weather taken for {location}.")
    weather = race_weather(location, race_day) if race_day and race_day >= date.today() else None
    temp_max = weather.get("temp_max") if weather and weather.get("available") else None
    altitude = (weather or {}).get("elevation_m") or 300

    athlete = _athlete_state()
    is_run = race_type in RUN_TYPES
    pacing = None if is_run else _cycling_pacing(distance, elevation, race_type, athlete, temp_max, altitude)
    run = _run_prediction(distance, elevation, athlete) if is_run else None
    hours = pacing["hours"] if pacing else (run["hours"] if run else distance / 10)
    if is_run and not run:
        assumptions.append("No recent 5 km+ run to base a prediction on - duration assumed at 10 km/h.")

    return {
        "race": race, "race_type": race_type, "priority": priority, "distance_km": distance,
        "elevation_m": elevation, "race_day": race_day, "location": location, "weather": weather,
        "athlete": athlete, "pacing": pacing, "run": run, "hours": hours,
        "hr_band": _hr_band(hours), "fuel": _fuelling(hours, temp_max, athlete["weight"], is_run),
        "taper": _taper(priority, athlete, race_day), "race_week": _race_week(priority, pacing),
        "assumptions": assumptions,
    }


def format_race_plan(p: dict) -> str:
    a, race = p["athlete"], p["race"]
    lines = [f"RACE PLAN DATA - {race.get('name') or 'Race'} ({p['priority']} race)"]
    when = f"{p['race_day']} (in {(p['race_day'] - date.today()).days} days)" if p["race_day"] else "date not set"
    lines.append(f"Course: {p['distance_km']:g} km, {p['elevation_m']:.0f} m elevation, "
                 f"{SURFACES.get(p['race_type'], {}).get('label', p['race_type'])}, {when}, location {p['location']}"
                 + (f", goal: {race['goal']}" if race.get("goal") else ""))
    lines.append(f"Athlete: FTP {a['ftp']:.0f} W (Intervals eFTP {a['eftp']:.0f} W)" if a["eftp"] else f"Athlete: FTP {a['ftp']:.0f} W")
    lines[-1] += (f", LTHR {a['lthr']:.0f} bpm, {a['weight']:g} kg ({a['ftp'] / a['weight']:.2f} W/kg)"
                  + (f", W' {a['w_prime'] / 1000:.1f} kJ" if a["w_prime"] else "")
                  + (f", CTL {a['ctl']:.0f} / ATL {a['atl']:.0f}" if a["ctl"] is not None else ""))
    if a["eftp"] and a["eftp"] < a["ftp"] * 0.95:
        lines.append(f"NOTE: eFTP is {100 - a['eftp'] / a['ftp'] * 100:.0f}% below the set FTP - if no recent maximal "
                     f"efforts explain it, targets based on eFTP would be {a['eftp'] / a['ftp'] * 100:.0f}% of those below.")

    w = p["weather"]
    lines.append("\nWEATHER: " + (w["summary"] if w else "no race date - weather not checked."))

    h_lo, h_hi = p["hr_band"]
    if p["pacing"]:
        pc = p["pacing"]
        lo, hi = p["hours"] * (1 - pc["spread"]), p["hours"] * (1 + pc["spread"])
        lines.append(f"\nPREDICTED TIME: {_hms(p['hours'])} (range {_hms(lo)}-{_hms(hi)}), avg {pc['speed']:.1f} km/h"
                     + (" assuming you ride in groups on the flats" if pc["surface"]["draft"] < 1 else ""))
        lines.append(f"Course model: {pc['climb_km']:.1f} km climbing at ~{pc['climb_grad']:.1f}% "
                     f"({_hms(pc['t_climb'])}), {pc['flat_km']:.1f} km flat/rolling ({_hms(pc['t_flat'])}), "
                     f"{pc['descent_km']:.1f} km descending ({_hms(pc['t_desc'])}).")
        if p["race_type"] not in a["power_types"]:
            lines.append(f"\nNO POWER METER: the athlete has no power data on {pc['surface']['label']} rides - pace this race "
                         "by HR and RPE (below). Watts are shown only as the model's reference for the time prediction.")
        lines.append(f"\nPACING: target IF {pc['target_if']:.2f} -> NP ~{pc['np']:.0f} W, average ~{pc['avg_w']:.0f} W, "
                     f"~{pc['kj']:.0f} kJ" + (f" (targets lowered {pc['heat_cut'] * 100:.0f}% for heat)" if pc["heat_cut"] else ""))
        lines.append(f"- Start (first 15-20 min): do not exceed ~{pc['flat_p'] * 1.1:.0f} W apart from the start surge; "
                     "early over-pacing is the #1 cause of fading.")
        lines.append(f"- Climbs: {pc['climb_p'] * 0.97:.0f}-{pc['climb_p'] * 1.03:.0f} W "
                     f"({pc['climb_p'] / a['ftp'] * 100:.0f}% FTP). Short kickers <2 min may go to "
                     f"{a['ftp'] * 1.1:.0f}-{a['ftp'] * 1.2:.0f} W, but each one spends W'.")
        lines.append(f"- Flats/rolling: {pc['flat_p'] * 0.95:.0f}-{pc['flat_p'] * 1.05:.0f} W; sit in wheels where possible.")
        lines.append("- Descents: recover (~35% FTP or coasting) and use them to eat and drink.")
        lines.append("- Final 20-25%: if HR is still within the cap and legs feel good, raise power 5-10%.")
    elif p["run"]:
        r = p["run"]
        lines.append(f"\nPREDICTED TIME: {_hms(r['hours'])} (Riegel from your best recent run: {r['reference']}; "
                     f"{r['effort_km']:.1f} effort-km including climbing)")
    lines.append(f"HR guide: average {h_lo * a['lthr']:.0f}-{h_hi * a['lthr']:.0f} bpm ({h_lo * 100:.0f}-{h_hi * 100:.0f}% LTHR); "
                 f"climbs may touch {min(h_hi + 0.04, 1.05) * a['lthr']:.0f} bpm. Use HR + RPE when power is unavailable "
                 "(MTB): RPE 6-7/10 for most of the race, 8 on key climbs.")

    f = p["fuel"]
    lines.append(f"\nFUELLING (heavy sweater, ~{f['temp']:.0f}°C):")
    lines.append(f"- Carbs: {f['carbs_h'][0]}-{f['carbs_h'][1]} g/h (total {f['carbs_total'][0]}-{f['carbs_total'][1]} g); "
                 "start eating within the first 20-30 min, little and often. 90+ g/h only if the gut has been trained to it.")
    lines.append(f"- Fluid: {f['fluid_h'][0]}-{f['fluid_h'][1]} ml/h ({f['fluid_total_l'][0]:.1f}-{f['fluid_total_l'][1]:.1f} L "
                 f"total, ~{f['bottles']} x 750 ml bottles - plan refills). Weigh before/after training rides "
                 "to calibrate: 1 kg lost = ~1 L sweat.")
    lines.append(f"- Sodium: {f['sodium_h'][0]}-{f['sodium_h'][1]} mg/h (electrolyte drink/capsules); preload "
                 "500 ml with ~1000 mg sodium 60-90 min before the start.")
    lines.append(f"- Caffeine (habitual high user): ~{f['caffeine_pre']} mg (3 mg/kg) 45-60 min before the start"
                 + (f"; top up {f['caffeine_top_up'][0]}-{f['caffeine_top_up'][1]} mg after about 2/3 of the race" if p["hours"] > 2 else "")
                 + f"; stay under ~{f['caffeine_max']} mg (6 mg/kg) total. Habitual users still benefit; only use "
                 "products already tested in training.")
    if f["carb_load"]:
        lines.append(f"- Carb loading: {f['carb_load'][0]}-{f['carb_load'][1]} g carbs/day for the 36-48 h before. "
                     f"Race breakfast {f['breakfast'][0]}-{f['breakfast'][1]} g carbs, 3 h before the start.")

    t = p["taper"]
    if t:
        lo, hi = t["target"]
        m = t["maintain"]
        lines.append(f"\nTAPER & FORM: {t['days']} training days to go; recommended taper {t['taper_days']} days "
                     f"(volume ~50%, keep short intensity). Target race-morning TSB for a {p['priority']} race: {lo:+d} to {hi:+d}.")
        lines.append(f"- Holding current load (~{a['weekly_load']:.0f} TSS/week) then tapering: race-morning CTL "
                     f"{m['ctl']:.0f}, TSB {m['tsb']:+.0f}.")
        if "build" in t:
            b = t["build"]
            lines.append(f"- Building ~7%/week first (to ~{b['pre_taper_weekly']:.0f} TSS/week before the taper): "
                         f"CTL {b['ctl']:.0f}, TSB {b['tsb']:+.0f}.")
        if "deeper" in t:
            d = t["deeper"]
            lines.append(f"- Projected TSB is below target; a deeper {d['taper_days']}-day taper at ~40% volume gives "
                         f"TSB {d['tsb']:+.0f} (CTL {d['ctl']:.0f}).")
    elif p["race_day"] and p["race_day"] < date.today():
        lines.append("\nThis race is in the past - use review_race for a post-race analysis.")

    lines.append("\nRACE WEEK:")
    lines += [f"- {d}" for d in p["race_week"]]
    try:
        lines.append("\nREFERENCE - " + format_past_races(past_races(5)))
    except Exception as e:
        logger.warning(f"Past races unavailable: {e}")
    if p["assumptions"]:
        lines.append("\nASSUMPTIONS: " + " ".join(p["assumptions"]))
    return "\n".join(lines)


def build_race_plan(
    race_id: str | None = None,
    distance_km: float | None = None,
    elevation_m: float | None = None,
    race_type: str | None = None,
    race_date: str | None = None,
    priority: str | None = None,
    location: str | None = None,
    goal: str | None = None,
    name: str | None = None,
) -> str:
    """Computes the full race plan data: predicted time, power/HR/RPE pacing per terrain, fuelling (carbs,
    fluid, sodium, caffeine), taper with projected race-morning form, race-week schedule, weather, and past
    races for reference.

    Args:
        race_id: id (or exact name) of a race from list_upcoming_races - its calendar details are used.
        distance_km: race distance; overrides the calendar value or describes a race not in the calendar.
        elevation_m: total elevation gain in metres.
        race_type: Ride (road), GravelRide, MountainBikeRide, VirtualRide, Run or TrailRun.
        race_date: YYYY-MM-DD.
        priority: A, B or C (A = main goal, gets a full taper).
        location: town/region for the weather lookup.
        goal: the athlete's goal, e.g. "sub 5h" or "top 10".
        name: race name (for races not in the calendar).
    """
    try:
        plan = compute_race_plan(race_id, distance_km=distance_km, elevation_m=elevation_m, type=race_type,
                                 date=race_date, priority=priority, location=location, goal=goal, name=name)
        return format_race_plan(plan)
    except ValueError as e:
        return str(e)
    except Exception as e:
        logger.error(f"Race plan failed: {e}", exc_info=True)
        return f"Race plan could not be computed: {e}"


# --- post-race review ---

def review_race(activity: str) -> str:
    """Post-race analysis: compares a completed race (activity external_id like 'i123456789', or a date
    YYYY-MM-DD) with what the pacing model recommends for that distance/elevation - intensity vs duration,
    HR vs LTHR, decoupling (fade), time in zones, W' use, fuelling (carbs logged), heat.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if activity.startswith("i"):
                cur.execute("SELECT * FROM activities WHERE external_id = %s;", (activity,))
            else:
                cur.execute("""
                    SELECT * FROM activities WHERE start_time::date = %s AND superseded_by IS NULL
                    ORDER BY race DESC, moving_time_sec DESC LIMIT 1;
                """, (activity,))
            act = cur.fetchone()
    finally:
        conn.close()
    if not act:
        return f"No activity found for '{activity}'."

    raw = act.get("raw_payload") or {}
    hours = (act["moving_time_sec"] or 0) / 3600
    distance = (act["distance_m"] or 0) / 1000
    ftp = act.get("ftp_at_time") or 250
    lthr = act.get("lthr") or 165
    lines = [f"RACE REVIEW DATA - {act['name']} ({act['type']}, {act['start_time']:%Y-%m-%d})",
             f"Result: {distance:.1f} km, {act.get('total_elevation_gain_m') or 0:.0f} m up, {_hms(hours)} moving "
             f"({distance / hours:.1f} km/h)" if hours else "Result: no moving time recorded"]
    ideal_if = _interp(IF_BY_HOURS, hours) if hours else None
    if act.get("intensity_factor") and ideal_if:
        actual_if = act["intensity_factor"]
        verdict = ("above what's sustainable for this duration - likely over-paced" if actual_if > ideal_if + 0.04
                   else "well below the model's target - conservative pacing, a tough day, or lots of drafting/coasting"
                   if actual_if < ideal_if - 0.08 else "in line with a well-paced effort for this duration")
        lines.append(f"Intensity: NP {act.get('normalized_power_w') or 0:.0f} W, avg {act.get('avg_power_w') or 0:.0f} W, "
                     f"IF {actual_if:.2f} vs model target {ideal_if:.2f} for {_hms(hours)} -> {verdict} (FTP then {ftp} W).")
    if act.get("avg_hr"):
        lo, hi = _hr_band(hours)
        share = act["avg_hr"] / lthr
        lines.append(f"Heart rate: avg {act['avg_hr']:.0f} bpm = {share * 100:.0f}% LTHR (model band {lo * 100:.0f}-{hi * 100:.0f}%), "
                     f"max {act.get('max_hr') or 0:.0f}.")
    if act.get("decoupling_pct") is not None:
        d = act["decoupling_pct"]
        lines.append(f"Decoupling: {d:.1f}% - " + ("strong finish, no cardiac drift." if d < 3 else
                     "some fade in the second half." if d < 7 else
                     "significant fade: pacing too hot early, heat, or under-fuelling/hydration."))
    zones = {z.get("id"): z.get("secs") or 0 for z in ((act.get("raw_metrics") or {}).get("icu_zone_times") or [])}
    total = sum(v for k, v in zones.items() if k and k.startswith("Z"))
    if total:
        lines.append("Power zones: " + ", ".join(f"{k} {zones[k] / total * 100:.0f}%" for k in sorted(zones) if k.startswith("Z") and zones[k]))
    if raw.get("icu_max_wbal_depletion") and raw.get("icu_w_prime"):
        lines.append(f"Max W' depletion: {raw['icu_max_wbal_depletion'] / 1000:.1f} kJ of {raw['icu_w_prime'] / 1000:.1f} kJ "
                     f"({raw['icu_max_wbal_depletion'] / raw['icu_w_prime'] * 100:.0f}%) - near 100% means the tank was emptied at some point.")
    if raw.get("icu_joules_above_ftp"):
        lines.append(f"Work above FTP: {raw['icu_joules_above_ftp'] / 1000:.0f} kJ.")
    carbs_in, carbs_used = raw.get("carbs_ingested"), raw.get("carbs_used")
    if carbs_used:
        lines.append(f"Carbs burned (Intervals estimate): {carbs_used} g" +
                     (f", ingested {carbs_in} g ({carbs_in / hours:.0f} g/h)" if carbs_in and hours else
                      " - carbs eaten not logged (log them in Intervals.icu to review fuelling)."))
    if raw.get("average_temp") is not None:
        lines.append(f"Temperature: avg {raw['average_temp']:.0f}°C" + (" - heat likely cost performance." if raw["average_temp"] >= 28 else "."))
    if act.get("feel") or act.get("rpe"):
        lines.append(f"Subjective: feel {act.get('feel')}, RPE {act.get('rpe')}.")
    if raw.get("interval_summary"):
        lines.append("Intervals detected: " + "; ".join(raw["interval_summary"][:8]))
    return "\n".join(lines)
