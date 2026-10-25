"""
Deterministic injury / overreaching risk checks, each judged against the
athlete's OWN baselines rather than textbook constants (e.g. gravel/MTB
cadence is naturally lower - what matters is a drop versus the athlete's
usual cadence for that ride type).

Used by:
- the injury_prevention agent (an LLM narrates this report),
- ai_coach's check_injury_risk tool (plain text, instant),
- the dashboard (structured dict).

Areas covered: training load (ACWR, TSB, ramp rate, monotony, weeks without
recovery), intensity distribution (time in zone vs a pyramidal split), recovery (HRV, resting HR, sleep vs baseline), cardiovascular drift
(aerobic decoupling, efficiency factor trend), biomechanics (cadence vs
baseline, L/R balance), subjective (feel/RPE vs objective data) and running
volume progression.
"""
import logging
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

from core.db import get_connection

logger = logging.getLogger("AI_COACH_V1.injury_checks")

CYCLING_TYPES = {"Ride", "GravelRide", "VirtualRide", "MountainBikeRide"}
POWER_GROUPS = {"Ride": "outdoor", "GravelRide": "outdoor", "VirtualRide": "indoor"}

# Nightly sleep the coaching treats as enough. 6.5 h, not the textbook 7, to fit the
# athlete's work schedule; below it means no intensity that day.
SLEEP_MIN_HOURS = 6.5

SEVERITY_ORDER = {"high": 0, "caution": 1, "ok": 2, "info": 3}

# Intervals.icu "feel": 1 Strong, 2 Good, 3 Normal, 4 Poor, 5 Weak.
FEEL_LABELS = {1: "Strong", 2: "Good", 3: "Normal", 4: "Poor", 5: "Weak"}
POOR_FEEL = 4

LOOKBACK_DAYS = 150
RECENT_DAYS = 14
TREND_RECENT_DAYS = 21


def _finding(severity, area, title, detail):
    return {"severity": severity, "area": area, "title": title, "detail": detail}


def _mean(values):
    return statistics.fmean(values) if values else None


def _sd(values):
    return statistics.stdev(values) if len(values) >= 2 else None


# --- data loading ---

def _load_data(conn, today: date) -> dict:
    since = today - timedelta(days=LOOKBACK_DAYS)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT external_id, start_time, type, name, moving_time_sec, distance_m, training_load,
                   intensity_factor, variability_index, decoupling_pct, efficiency_factor,
                   avg_cadence, avg_lr_balance, avg_power_w, feel, rpe,
                   raw_metrics->'icu_zone_times' AS zone_times, raw_payload->'icu_hr_zone_times' AS hr_zone_times
            FROM activities
            WHERE superseded_by IS NULL AND start_time >= %s
            ORDER BY start_time;
        """, (since,))
        activities = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT day, ctl, atl, ramp_rate, hrv, resting_hr, sleep_duration_sec, soreness, fatigue, injury
            FROM daily_metrics WHERE day >= %s ORDER BY day;
        """, (since,))
        wellness = [dict(r) for r in cur.fetchall()]
    for a in activities:
        a["day"] = a["start_time"].date() if isinstance(a["start_time"], datetime) else a["start_time"]
    return {"activities": activities, "wellness": wellness}


def _label(a) -> str:
    return f"{a['type']} {a['day']} ({a.get('name') or 'untitled'})"


# --- load ---

def _daily_loads(activities, start: date, end: date) -> list[float]:
    by_day = defaultdict(float)
    for a in activities:
        by_day[a["day"]] += float(a.get("training_load") or 0)
    days = (end - start).days + 1
    return [by_day[start + timedelta(days=i)] for i in range(days)]


def check_load(data, today, findings, checked, gaps):
    rows = [w for w in data["wellness"] if w["ctl"] is not None and w["atl"] is not None]
    if not rows:
        gaps.append("No CTL/ATL data - training load checks skipped.")
        return
    latest = rows[-1]
    ctl, atl = float(latest["ctl"]), float(latest["atl"])
    tsb = ctl - atl
    checked.append("training load (ACWR, TSB, ramp rate, monotony, recovery weeks)")

    if ctl >= 10:
        acwr = atl / ctl
        if acwr >= 1.5:
            findings.append(_finding("high", "Load", f"Acute:chronic load ratio {acwr:.2f}",
                f"Fatigue (ATL {atl:.0f}) is {acwr:.2f}x fitness (CTL {ctl:.0f}). Ratios above 1.5 are strongly "
                "associated with injury and illness - load is spiking faster than the body has adapted to."))
        elif acwr >= 1.3:
            findings.append(_finding("caution", "Load", f"Acute:chronic load ratio {acwr:.2f}",
                f"ATL {atl:.0f} vs CTL {ctl:.0f}. Above ~1.3 is the start of the elevated-risk zone; fine for a planned "
                "overload week, but don't stack another one on top."))
        else:
            findings.append(_finding("ok", "Load", f"Acute:chronic load ratio {acwr:.2f}",
                "Within the 0.8-1.3 range where load progression is well tolerated."))

    if tsb <= -30:
        findings.append(_finding("high", "Load", f"Form (TSB) {tsb:+.0f}",
            "Deep fatigue - beyond about -30, overreaching and injury risk rise sharply. Recovery days needed."))
    elif tsb <= -20:
        findings.append(_finding("caution", "Load", f"Form (TSB) {tsb:+.0f}",
            "Productive but heavy training zone; keep an eye on HRV and how sessions feel."))

    ramp = latest.get("ramp_rate")
    if ramp is not None:
        ramp = float(ramp)
        if ramp > 12:
            findings.append(_finding("high", "Load", f"Ramp rate {ramp:+.1f} CTL/week",
                "Fitness is being built very aggressively; sustained ramps above ~8/week often end in illness or injury."))
        elif ramp > 8:
            findings.append(_finding("caution", "Load", f"Ramp rate {ramp:+.1f} CTL/week",
                "Above the ~5-8/week range most athletes can sustain for more than a couple of weeks."))

    # Foster monotony over the last 7 days (incl. rest days as zero load).
    week = _daily_loads(data["activities"], today - timedelta(days=6), today)
    if sum(week) > 0:
        sd = statistics.pstdev(week)
        if sd > 0:
            monotony = statistics.fmean(week) / sd
            strain = sum(week) * monotony
            if monotony > 2.0:
                findings.append(_finding("caution", "Load", f"Training monotony {monotony:.1f}",
                    f"Very similar load every day (weekly strain {strain:.0f}). Foster's research links monotony >2.0 "
                    "with illness/overuse - add contrast: real easy days and fewer medium days."))

    # Consecutive calendar weeks (Mon-Sun, same as the weekly chart) without a recovery week
    # (load <= 75% of the prior 3 weeks' average). Only completed weeks: the current partial
    # week would always look like a recovery week early on.
    this_monday = today - timedelta(days=today.weekday())
    blocks = []
    for i in range(1, 11):
        start = this_monday - timedelta(days=7 * i)
        blocks.append(sum(_daily_loads(data["activities"], start, start + timedelta(days=6))))
    blocks.reverse()  # oldest -> newest
    streak = 0
    for i in range(len(blocks) - 1, 2, -1):
        prior = blocks[i - 3:i]
        if statistics.fmean(prior) > 0 and blocks[i] <= 0.75 * statistics.fmean(prior):
            break
        streak += 1
    if streak >= 6:
        findings.append(_finding("high", "Load", f"{streak} weeks without a recovery week",
            "Cumulative fatigue builds even when single weeks look fine - schedule a lighter week (~50-60% load) now."))
    elif streak >= 4:
        findings.append(_finding("caution", "Load", f"{streak} weeks without a recovery week",
            "Most periodised plans insert a recovery week every 3-4 weeks; one is due."))


# --- intensity distribution ---

# Three-zone model: low = below the first threshold (power Z1-Z2 / HR Z1-Z2), moderate =
# tempo to threshold (Z3-Z4), high = above threshold (Z5+). Pyramidal, as pro cyclists
# train: most time low, less moderate, least high (Tour top-5 riders ~81/13/6).
TID_DAYS = 28
TID_MIN_HOURS = 5
TID_LOW_MIN, TID_MODERATE_MAX, TID_HIGH_MAX = 70, 25, 12   # % of time
NOT_ENDURANCE = {"WeightTraining", "Walk", "Yoga", "Workout"}


def _three_zones(a) -> tuple[float, float, float] | None:
    """(low, moderate, high) seconds from power zones if the activity has them, else HR zones."""
    power = {z["id"]: z.get("secs") or 0 for z in (a.get("zone_times") or []) if isinstance(z, dict)}
    if sum(v for k, v in power.items() if k != "SS"):  # SS overlaps Z3/Z4
        return (power.get("Z1", 0) + power.get("Z2", 0), power.get("Z3", 0) + power.get("Z4", 0),
                sum(power.get(z, 0) for z in ("Z5", "Z6", "Z7")))
    hr = [s or 0 for s in (a.get("hr_zone_times") or [])]
    if sum(hr):
        return sum(hr[:2]), sum(hr[2:4]), sum(hr[4:])
    return None


def intensity_distribution(activities, today: date) -> dict | None:
    """Time-in-zone split over the last TID_DAYS: {low, moderate, high} in %, hours."""
    since = today - timedelta(days=TID_DAYS)
    totals = [0.0, 0.0, 0.0]
    for a in activities:
        if a["day"] < since or a["type"] in NOT_ENDURANCE:
            continue
        z = _three_zones(a)
        if z:
            totals = [t + v for t, v in zip(totals, z)]
    secs = sum(totals)
    if secs < TID_MIN_HOURS * 3600:
        return None
    low, mod, high = (round(100 * t / secs) for t in totals)
    return {"low": low, "moderate": mod, "high": high, "hours": round(secs / 3600, 1), "days": TID_DAYS}


def check_intensity(data, today, findings, checked, gaps):
    tid = intensity_distribution(data["activities"], today)
    if not tid:
        gaps.append(f"Under {TID_MIN_HOURS} h with zone data in the last {TID_DAYS} days - intensity distribution skipped.")
        return
    checked.append("intensity distribution (time in zone vs a pyramidal split)")
    split = f"{tid['low']}% easy / {tid['moderate']}% moderate / {tid['high']}% hard over {tid['hours']} h"
    if tid["moderate"] > TID_MODERATE_MAX:
        findings.append(_finding("caution", "Intensity", f"Too much moderate intensity ({tid['moderate']}%)",
            f"Last {tid['days']} days: {split}. A pyramidal plan keeps tempo/threshold under ~{TID_MODERATE_MAX}%: "
            "make easy rides truly easy (Zone 2 or below) so the quality sessions can be quality."))
    elif tid["low"] < TID_LOW_MIN:
        findings.append(_finding("caution", "Intensity", f"Too little easy time ({tid['low']}%)",
            f"Last {tid['days']} days: {split}. Aim for {TID_LOW_MIN}-85% easy - the aerobic base comes from it."))
    elif tid["high"] > TID_HIGH_MAX:
        findings.append(_finding("caution", "Intensity", f"A lot of hard work ({tid['high']}%)",
            f"Last {tid['days']} days: {split}. Above ~{TID_HIGH_MAX}% hard, recovery usually can't keep up - "
            "keep it to 1-2 hard sessions a week."))
    else:
        findings.append(_finding("info", "Intensity", "Intensity distribution is pyramidal", f"Last {tid['days']} days: {split}."))


# --- recovery ---

def check_recovery(data, today, findings, checked, gaps):
    wellness = data["wellness"]

    def split(field, transform=lambda v: float(v)):
        recent = [transform(w[field]) for w in wellness if w[field] is not None and w["day"] > today - timedelta(days=7)]
        base = [transform(w[field]) for w in wellness
                if w[field] is not None and today - timedelta(days=67) < w["day"] <= today - timedelta(days=7)]
        return recent, base

    hrv_recent, hrv_base = split("hrv")
    if len(hrv_recent) >= 3 and len(hrv_base) >= 20:
        checked.append("HRV vs 60-day baseline")
        mean, sd = _mean(hrv_base), _sd(hrv_base)
        r = _mean(hrv_recent)
        if sd and r < mean - sd:
            findings.append(_finding("high", "Recovery", f"HRV well below baseline ({r:.0f} vs {mean:.0f} ms)",
                f"7-day average is more than 1 SD (±{sd:.0f}) under your 60-day norm - a classic sign of accumulated "
                "fatigue, illness coming on, or life stress. Prioritise easy days and sleep."))
        elif sd and r < mean - 0.5 * sd:
            findings.append(_finding("caution", "Recovery", f"HRV trending low ({r:.0f} vs {mean:.0f} ms)",
                f"7-day average has dropped below your normal range (baseline {mean:.0f} ±{0.5 * sd:.0f} ms)."))
        else:
            findings.append(_finding("ok", "Recovery", f"HRV in normal range ({r:.0f} vs {mean:.0f} ms)",
                "7-day average is within your usual range."))
        latest = [w for w in wellness if w["hrv"] is not None][-1]
        if sd and latest["day"] >= today - timedelta(days=1) and float(latest["hrv"]) < mean - 1.5 * sd:
            findings.append(_finding("caution", "Recovery", f"Acute HRV dip on {latest['day']} ({latest['hrv']:.0f} ms)",
                "A single very low reading - consider swapping today's hard session for an easy one."))
    else:
        gaps.append("Not enough HRV readings for a baseline comparison.")

    rhr_recent, rhr_base = split("resting_hr")
    if len(rhr_recent) >= 3 and len(rhr_base) >= 15:
        checked.append("resting HR vs baseline")
        diff = _mean(rhr_recent) - _mean(rhr_base)
        if diff >= 5:
            findings.append(_finding("high", "Recovery", f"Resting HR +{diff:.0f} bpm above baseline",
                "An elevated resting HR for several days often precedes illness or signals overreaching."))
        elif diff >= 3:
            findings.append(_finding("caution", "Recovery", f"Resting HR +{diff:.0f} bpm above baseline",
                "Slightly elevated - watch for it climbing further."))
    else:
        gaps.append("Resting HR is recorded too sparsely for a trend (wear the watch overnight more often).")

    sleep_recent, _ = split("sleep_duration_sec", lambda v: float(v) / 3600)
    if len(sleep_recent) >= 3:
        checked.append("sleep duration")
        avg = _mean(sleep_recent)
        if avg < 6:
            findings.append(_finding("high", "Recovery", f"Average sleep {avg:.1f} h",
                "Under 6 h/night markedly raises injury risk in endurance athletes; recovery can't keep up with training."))
        elif avg < SLEEP_MIN_HOURS:
            findings.append(_finding("caution", "Recovery", f"Average sleep {avg:.1f} h",
                f"Below the {SLEEP_MIN_HOURS:g} h minimum needed to absorb training."))
    else:
        gaps.append("Sleep data is too sparse in the last week.")

    for field, label in (("soreness", "soreness"), ("fatigue", "fatigue"), ("injury", "injury")):
        recent = [w for w in wellness if w[field] is not None and w["day"] > today - timedelta(days=3)]
        if recent and float(recent[-1][field]) >= 3:
            findings.append(_finding("caution", "Recovery", f"Self-reported {label} {recent[-1][field]} on {recent[-1]['day']}",
                "You logged this in Intervals.icu wellness - take it seriously alongside the objective data."))


# --- cardiovascular drift ---

def _aerobic_share(a):
    """(Z1+Z2 share, Z4+ share) of power-zone time, or None without zone data. The
    'SS' (sweet spot) bucket overlaps Z3/Z4 in Intervals.icu, so it's excluded."""
    zones = {z.get("id"): z.get("secs") or 0 for z in (a.get("zone_times") or []) if isinstance(z, dict)}
    total = sum(v for k, v in zones.items() if k and k.startswith("Z"))
    if not total:
        return None
    low = zones.get("Z1", 0) + zones.get("Z2", 0)
    high = sum(zones.get(f"Z{i}", 0) for i in range(4, 8))
    return low / total, high / total


def _steady(a, min_minutes):
    """Steady aerobic ride: long enough, and mostly Z1-Z2 by power zones (decoupling and
    EF are only meaningful there - interval sessions inflate HR drift by design).
    Falls back to IF/VI limits when zone data is missing."""
    if (a.get("moving_time_sec") or 0) < min_minutes * 60:
        return False
    shares = _aerobic_share(a)
    if shares is not None:
        low, high = shares
        return low >= 0.65 and high <= 0.10
    if a.get("intensity_factor") is not None and a["intensity_factor"] > 0.85:
        return False
    if a.get("variability_index") is not None and a["variability_index"] > 1.2:
        return False
    return True


def check_drift(data, today, findings, checked, gaps):
    acts = data["activities"]
    # Strongly negative decoupling is not physiological - usually an HR strap dropout.
    # Strongly negative decoupling is not physiological - usually an HR strap dropout.
    eligible = [a for a in acts if a.get("decoupling_pct") is not None
                and -10 <= a["decoupling_pct"] <= 30 and _steady(a, 60)]
    recent = [a for a in eligible if a["day"] > today - timedelta(days=TREND_RECENT_DAYS)][-4:]
    if recent:
        checked.append("aerobic decoupling (HR drift vs power) on steady aerobic rides 60min+")
        high = [a for a in recent if a["decoupling_pct"] > 5]
        details = "; ".join(f"{_label(a)}: {a['decoupling_pct']:.1f}%" for a in recent)
        if len(high) >= 3:
            findings.append(_finding("high", "Cardio drift", f"HR drifting on {len(high)} of last {len(recent)} steady rides",
                f"Decoupling above 5% means heart rate climbs while power stays flat - repeated, it points to fatigue, "
                f"dehydration/heat, or an aerobic base that's eroding. {details}"))
        elif len(high) >= 2:
            findings.append(_finding("caution", "Cardio drift", f"HR drifting on {len(high)} of last {len(recent)} steady rides",
                f"Decoupling >5% on several steady rides. Check heat and hydration first; if those were fine, it's fatigue. {details}"))
        elif high and high[-1] is recent[-1] and high[-1]["decoupling_pct"] > 10:
            a = high[-1]
            findings.append(_finding("caution", "Cardio drift", f"Large HR drift on latest steady ride ({a['decoupling_pct']:.1f}%)",
                f"{_label(a)}. One-off >10% drift is usually heat, dehydration or under-fuelling - worth checking."))
        else:
            findings.append(_finding("ok", "Cardio drift", "Heart rate tracking power well on steady rides",
                f"Decoupling within ~5% on recent steady rides. {details}"))
    else:
        gaps.append("No steady 60min+ rides with decoupling data in the last 3 weeks.")

    # Efficiency factor (NP/HR) trend per power group, recent 3 weeks vs prior baseline.
    by_group = defaultdict(lambda: {"recent": [], "base": []})
    for a in acts:
        group = POWER_GROUPS.get(a["type"])
        ef = a.get("efficiency_factor")
        if not group or ef is None or not _steady(a, 45):
            continue
        if a.get("intensity_factor") is not None and a["intensity_factor"] < 0.55:
            continue
        bucket = "recent" if a["day"] > today - timedelta(days=TREND_RECENT_DAYS) else "base"
        by_group[group][bucket].append(float(ef))
    for group, vals in by_group.items():
        if len(vals["recent"]) >= 2 and len(vals["base"]) >= 5:
            checked.append(f"efficiency factor trend ({group})")
            change = (_mean(vals["recent"]) / _mean(vals["base"]) - 1) * 100
            if change <= -10:
                findings.append(_finding("high", "Cardio drift", f"{group.title()} efficiency down {abs(change):.0f}%",
                    f"Power per heartbeat on steady {group} rides fell from {_mean(vals['base']):.2f} to "
                    f"{_mean(vals['recent']):.2f} - fatigue or illness is the usual cause."))
            elif change <= -5:
                findings.append(_finding("caution", "Cardio drift", f"{group.title()} efficiency down {abs(change):.0f}%",
                    f"Steady-ride NP/HR is {_mean(vals['recent']):.2f} vs your usual {_mean(vals['base']):.2f}. "
                    "Heat can explain this; otherwise it's an early fatigue marker."))
            elif change >= 5:
                findings.append(_finding("info", "Cardio drift", f"{group.title()} efficiency up {change:.0f}%",
                    "More power for the same heart rate - aerobic fitness is improving."))


# --- biomechanics ---

def check_biomechanics(data, today, findings, checked, gaps):
    acts = data["activities"]
    recent_cut = today - timedelta(days=RECENT_DAYS)

    for sport in sorted(CYCLING_TYPES | {"Run"}):
        with_cad = [a for a in acts if a["type"] == sport and a.get("avg_cadence")]
        base = [float(a["avg_cadence"]) for a in with_cad if a["day"] <= recent_cut and (a.get("moving_time_sec") or 0) >= 1200]
        recent = [a for a in with_cad if a["day"] > recent_cut and (a.get("moving_time_sec") or 0) >= 1200]
        if not recent:
            continue
        if len(base) < 5:
            gaps.append(f"Not enough {sport} history to set a personal cadence baseline.")
            continue
        checked.append(f"{sport} cadence vs personal baseline")
        baseline = statistics.median(base)
        unit = "spm" if sport == "Run" else "rpm"
        low = [a for a in recent if float(a["avg_cadence"]) < baseline - 10]
        recent_mean = _mean([float(a["avg_cadence"]) for a in recent])
        if low:
            details = "; ".join(f"{_label(a)}: {a['avg_cadence']:.0f}" for a in low)
            findings.append(_finding("caution", "Biomechanics", f"{sport} cadence well below your norm",
                f"Your usual {sport} cadence is ~{baseline:.0f} {unit}; these were 10+ lower: {details}. Sustained "
                "low-cadence/high-torque riding loads the knees (patellofemoral) - fine for occasional strength work, "
                "worth watching if unplanned." if sport != "Run" else
                f"Your usual run cadence is ~{baseline:.0f}; these were 10+ lower: {details}. Lower cadence usually "
                "means overstriding and higher impact per step."))
        elif len(recent) >= 3 and recent_mean < baseline - 5:
            findings.append(_finding("caution", "Biomechanics", f"{sport} cadence trending down",
                f"Recent average {recent_mean:.0f} {unit} vs your usual {baseline:.0f}. Often a fatigue sign."))

    lr = [a for a in acts if a.get("avg_lr_balance") is not None and a["day"] > today - timedelta(days=TREND_RECENT_DAYS)]
    if len(lr) >= 3:
        checked.append("left/right power balance")
        avg = _mean([float(a["avg_lr_balance"]) for a in lr])
        dev = abs(avg - 50)
        side = "left" if avg > 50 else "right"
        if dev > 3.5:
            findings.append(_finding("high", "Biomechanics", f"L/R balance {avg:.1f}/{100 - avg:.1f}",
                f"Consistent {dev:.1f}% bias toward the {side} leg over {len(lr)} rides - a compensation pattern "
                "worth a bike-fit or physio check."))
        elif dev > 2:
            findings.append(_finding("caution", "Biomechanics", f"L/R balance {avg:.1f}/{100 - avg:.1f}",
                f"Mild {side}-leg bias over {len(lr)} rides; watch whether it grows."))
        else:
            findings.append(_finding("ok", "Biomechanics", f"L/R balance {avg:.1f}/{100 - avg:.1f}",
                "Within ±2% - symmetrical."))
    else:
        gaps.append("Too few recent rides with L/R balance data (only dual-sided power meters record it).")


# --- subjective vs objective ---

def check_subjective(data, today, findings, checked, gaps):
    recent = [a for a in data["activities"] if a["day"] > today - timedelta(days=RECENT_DAYS)]
    with_feel = [a for a in recent if a.get("feel") is not None or a.get("rpe") is not None]
    if not with_feel:
        gaps.append("No feel/RPE logged recently - rating sessions in Intervals.icu makes fatigue checks much better.")
        return
    checked.append("feel/RPE vs objective data")
    for a in with_feel:
        feel = a.get("feel")
        dec = a.get("decoupling_pct")
        if feel is not None and feel >= POOR_FEEL and dec is not None and dec > 5:
            findings.append(_finding("caution", "Subjective", f"Felt {FEEL_LABELS.get(feel, feel)} with {dec:.1f}% HR drift",
                f"{_label(a)}: poor feel plus cardiovascular drift - systemic fatigue rather than just a bad day."))
        rpe, intensity = a.get("rpe"), a.get("intensity_factor")
        if rpe is not None and intensity is not None and rpe >= 8 and intensity < 0.75:
            findings.append(_finding("caution", "Subjective", f"RPE {rpe} on an easy-intensity session",
                f"{_label(a)}: IF {intensity:.2f} should feel moderate; a hard RPE suggests fatigue."))
    poor = [a for a in with_feel if (a.get("feel") or 0) >= POOR_FEEL]
    if len(poor) >= 3:
        findings.append(_finding("caution", "Subjective", f"{len(poor)} sessions felt Poor/Weak in 2 weeks",
            "Repeated poor sessions are one of the earliest overreaching signs."))


# --- running volume ---

def check_running(data, today, findings, checked, gaps):
    runs = [a for a in data["activities"] if a["type"] in ("Run", "TrailRun") and a.get("distance_m")]
    if not runs:
        return
    def km(start, end):
        return sum(float(a["distance_m"]) for a in runs if start < a["day"] <= end) / 1000
    this_week = km(today - timedelta(days=7), today)
    prior_avg = km(today - timedelta(days=35), today - timedelta(days=7)) / 4
    if this_week == 0:
        return
    checked.append("running volume progression")
    if prior_avg >= 5:
        change = (this_week / prior_avg - 1) * 100
        if change > 50:
            findings.append(_finding("high", "Running", f"Run volume +{change:.0f}% ({this_week:.0f} km vs {prior_avg:.0f} km/wk)",
                "Running loads bone and tendon far more than cycling and they adapt slowly - jumps above ~30% are a "
                "common route to shin splints, Achilles and stress injuries."))
        elif change > 30:
            findings.append(_finding("caution", "Running", f"Run volume +{change:.0f}% ({this_week:.0f} km vs {prior_avg:.0f} km/wk)",
                "Faster increase than tendons usually tolerate; hold this level for a week before adding more."))
    elif this_week > 15:
        findings.append(_finding("caution", "Running", f"{this_week:.0f} km run this week from a low base",
            f"Your previous 4 weeks averaged {prior_avg:.0f} km - cardio fitness from cycling outpaces "
            "running-specific tissue tolerance. Build run volume gradually."))


# --- public API ---

def run_injury_checks(today: date | None = None) -> dict:
    today = today or date.today()
    conn = get_connection()
    try:
        data = _load_data(conn, today)
    finally:
        conn.close()

    findings, checked, gaps = [], [], []
    for check in (check_load, check_intensity, check_recovery, check_drift, check_biomechanics, check_subjective,
                  check_running):
        try:
            check(data, today, findings, checked, gaps)
        except Exception as e:
            logger.error(f"Injury check {check.__name__} failed: {e}", exc_info=True)
            gaps.append(f"{check.__name__} failed: {e}")

    findings.sort(key=lambda f: SEVERITY_ORDER[f["severity"]])
    counts = defaultdict(int)
    for f in findings:
        counts[f["severity"]] += 1
    overall = "high" if counts["high"] else "caution" if counts["caution"] else "ok"
    return {
        "date": today.isoformat(),
        "overall": overall,
        "counts": dict(counts),
        "findings": findings,
        "checked": checked,
        "data_gaps": gaps,
    }


def format_injury_report(result: dict) -> str:
    """Plain-text report grouped by severity (so a small local LLM narrating it
    can't regroup findings under the wrong level)."""
    lines = [f"Injury/overreaching risk check for {result['date']} - OVERALL: {result['overall'].upper()}"]
    sections = (("high", "HIGH RISK"), ("caution", "CAUTION"), ("ok", "OK"), ("info", "POSITIVE"))
    for severity, heading in sections:
        items = [f for f in result["findings"] if f["severity"] == severity]
        lines.append(f"\n{heading} ({len(items)}):")
        lines += [f"- {f['area']}: {f['title']} - {f['detail']}" for f in items] or ["- none"]
    lines.append("\nChecked: " + (", ".join(result["checked"]) or "nothing (no data)"))
    if result["data_gaps"]:
        lines.append("Data gaps: " + " ".join(result["data_gaps"]))
    return "\n".join(lines)


def format_injury_markdown(result: dict) -> str:
    """User-facing rendering of the report (chat / Telegram), built deterministically
    so severity grouping and numbers are always exactly what the checks computed."""
    verdict = {"high": "🔴 HIGH RISK", "caution": "🟠 CAUTION", "ok": "🟢 LOOKING GOOD"}[result["overall"]]
    lines = [f"**Overall: {verdict}**"]
    sections = (("high", "🔴 High risk"), ("caution", "🟠 Caution"), ("ok", "🟢 Looks good"), ("info", "📈 Positive trends"))
    for severity, heading in sections:
        items = [f for f in result["findings"] if f["severity"] == severity]
        if not items:
            continue
        lines.append(f"\n**{heading}**")
        if severity in ("high", "caution"):
            lines += [f"- **{f['area']} - {f['title']}**: {f['detail']}" for f in items]
        else:
            lines += [f"- {f['area']}: {f['title']}" for f in items]
    if result["data_gaps"]:
        lines.append("\n**Data gaps**")
        lines += [f"- {g}" for g in result["data_gaps"]]
    return "\n".join(lines)
