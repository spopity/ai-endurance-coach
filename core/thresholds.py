"""
Thresholds measured from the athlete's own data instead of fixed % of FTP / test protocols:

LT1 (aerobic threshold) from DFA-alpha1 - the fractal correlation of the beat-to-beat (RR)
intervals falls with intensity and crosses 0.75 at about the first lactate / ventilatory
threshold (Rogers et al., Front. Physiol. 2021; Gronwald et al. 2020). Intervals.icu stores the
RR intervals of every activity recorded with a chest strap (the "hrv" stream). Each activity is
cut into 2-min windows (from minute 10 on: no warm-up transients), every 60 s; a window counts
when it is stationary (heart rate of its two halves within STATIONARY_BPM) and clean (at most
MAX_ARTIFACTS of its beats more than 20% off the local median - those beats are dropped).
The windows of the last LOOKBACK_DAYS are pooled per sport and binned by heart rate (and by
power on the bike); LT1 is where the binned median alpha1 first falls through 0.75. Pooling
makes it robust - one ride with a few odd windows doesn't move it. Quality gate: the curve must
start well above (>= 1.0) and must not climb back over REBOUND after the crossing - noisy RR
makes alpha1 rebound at high heart rates, and such data is reported as unreliable rather than used.

Running has its own confounder: heart rate locks to the step rate (cardiolocomotor coupling) when
the two are close - at ~160 steps/min that is exactly the threshold range, and alpha1 climbs back
towards 1. Run windows with heart rate within COUPLING_BPM of the step rate are dropped, and a run
LT1 more than RUN_BELOW_BIKE_BPM under the bike LT1 isn't trusted (running thresholds sit at or a
little above cycling ones); easy runs then keep the HR-zone cap.

Zone 2 is anchored on it: its top is 95% of the LT1 power (just under LT1, with room for drift),
kept within Z2_TOP_BOUNDS of the FTP; without a reliable LT1 it stays the classic 75% of FTP.
The FTP test's warm-up is a 4-step ramp, so every test also refreshes LT1.

Critical speed (running): from a CS test (3 min and 9 min all-out, Intervals.icu's pace curve
of that day): CS = (D9 - D3) / 360 s, D' = D3 - CS x 180 s (the 2-parameter model; ~ the pace
held for 30-40 min). Without a test, Intervals.icu's model over the last 90 days of training is
shown as a floor - training runs aren't all-out, so it underestimates and isn't used for paces.

update() runs nightly (scripts/update_plan.py): it fetches the RR streams of new activities
once (cached per activity in state/dfa_windows.json) and writes state/thresholds.json, which
current() reads cheaply.
"""
import json
import logging
import math
import os
import statistics
from datetime import date, datetime, timedelta

import requests

from core.db import load_env

logger = logging.getLogger("AI_COACH_V1.thresholds")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state", "thresholds.json")
WINDOWS_CACHE = os.path.join(ROOT, "state", "dfa_windows.json")
API = "https://intervals.icu/api/v1"

WINDOW_SEC, STEP_SEC, SKIP_SEC = 120, 60, 600
MIN_BEATS = 150
MAX_ARTIFACTS = 0.03
STATIONARY_BPM = 4
LT1_ALPHA = 0.75
START_ABOVE = 1.0        # the lowest bins must be clearly aerobic...
REBOUND = 0.9            # ...and no bin past the crossing may climb back over this
HR_BIN, WATT_BIN = 4, 20
MIN_BIN = 5              # windows per bin
MIN_ACTIVITIES = 4
COUPLING_BPM = 20        # run windows with heart rate this close to the step rate are dropped
RUN_BELOW_BIKE_BPM = 5   # a run LT1 further below the bike LT1 isn't trusted
CACHE_VERSION = 2        # windows: [hr, alpha1, watts, cadence]
LOOKBACK_DAYS = 90
Z2_TOP_DEFAULT = 0.75
Z2_TOP_BOUNDS = (0.70, 0.82)
LT1_PLAUSIBLE = (0.60, 0.95)   # LT1 power as a share of FTP
CS_TEST_DAYS = 120             # a CS test this old still sets the run paces
CS_PLAUSIBLE = (2.0, 6.5)      # m/s
D_PRIME_PLAUSIBLE = (50, 450)  # m
RIDE_TYPES = ("Ride", "GravelRide", "MountainBikeRide", "VirtualRide")
RUN_TYPES = ("Run", "TrailRun")


# --- DFA alpha1 ---

def _slope(xs: list[float], ys: list[float]) -> float:
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)


def dfa_a1(rr: list[float]) -> float:
    """Short-term scaling exponent (box sizes 4-16 beats) of an RR series in ms."""
    mean, acc, profile = statistics.fmean(rr), 0.0, []
    for v in rr:
        acc += v - mean
        profile.append(acc)
    log_n, log_f = [], []
    for n in range(4, 17):
        mid = (n - 1) / 2
        sxx = sum((i - mid) ** 2 for i in range(n))
        total, count = 0.0, 0
        for b in range(len(profile) // n):
            seg = profile[b * n:(b + 1) * n]
            m = sum(seg) / n
            k = sum((i - mid) * (v - m) for i, v in enumerate(seg)) / sxx
            total += sum((v - m - k * (i - mid)) ** 2 for i, v in enumerate(seg))
            count += n
        if not count or total <= 0:
            continue
        log_n.append(math.log(n))
        log_f.append(math.log(math.sqrt(total / count)))
    return _slope(log_n, log_f) if len(log_n) >= 4 else float("nan")


def windows(hrv: list, watts: list | None = None, cadence: list | None = None) -> list[list[float]]:
    """[[heart rate, alpha1, watts or None, cadence or None], ...] of the clean, stationary 2-min windows.
    `hrv` is Intervals.icu's per-second list of the RR intervals (ms) that ended in that second."""
    beats = [(i, float(v)) for i, sec in enumerate(hrv or []) if sec for v in sec if v]
    if len(beats) < MIN_BEATS:
        return []
    t, rr = [b[0] for b in beats], [b[1] for b in beats]
    bad = []
    for i, v in enumerate(rr):
        med = statistics.median(rr[max(0, i - 5):i + 6])
        bad.append(abs(v - med) / med > 0.2)
    out, lo = [], 0
    for start in range(SKIP_SEC, t[-1] - WINDOW_SEC, STEP_SEC):
        while lo < len(t) and t[lo] < start:
            lo += 1
        hi = lo
        while hi < len(t) and t[hi] < start + WINDOW_SEC:
            hi += 1
        n = hi - lo
        if n < MIN_BEATS or sum(bad[lo:hi]) / n > MAX_ARTIFACTS:
            continue
        clean = [rr[i] for i in range(lo, hi) if not bad[i]]
        half = len(clean) // 2
        if abs(60000 / statistics.fmean(clean[:half]) - 60000 / statistics.fmean(clean[half:])) > STATIONARY_BPM:
            continue
        a1 = dfa_a1(clean)
        if math.isnan(a1):
            continue
        w = [x for x in (watts or [])[start:start + WINDOW_SEC] if x is not None]
        c = [x for x in (cadence or [])[start:start + WINDOW_SEC] if x]
        out.append([round(60000 / statistics.fmean(clean), 1), round(a1, 3), round(statistics.fmean(w)) if w else None,
                    round(statistics.fmean(c), 1) if c else None])
    return out


def crossing(points: list[tuple[float, float]], width: float) -> tuple[float | None, str]:
    """(x where the binned median alpha1 first falls through 0.75, reason). `points` are (x, alpha1)."""
    bins: dict[int, list[float]] = {}
    for x, a1 in points:
        bins.setdefault(int(x // width), []).append(a1)
    curve = [((b + 0.5) * width, statistics.median(v)) for b, v in sorted(bins.items()) if len(v) >= MIN_BIN]
    if len(curve) < 4:
        return None, "too few steady stretches across intensities"
    if curve[0][1] < START_ABOVE:
        return None, "no easy stretches clearly below LT1"
    for i in range(1, len(curve)):
        (x0, a0), (x1, a1) = curve[i - 1], curve[i]
        if a1 <= LT1_ALPHA:
            if any(a > REBOUND for _, a in curve[i + 1:]):
                return None, "alpha1 climbs back at high heart rates (noisy RR data)"
            return round(x0 + (a0 - LT1_ALPHA) / (a0 - a1) * (x1 - x0), 1), "ok"
    return None, "no stretches hard enough to cross LT1"


def coupled(w: list) -> bool:
    """A run window whose heart rate is within COUPLING_BPM of the step rate (cadence is per leg
    below 120, steps above)."""
    cad = w[3] if len(w) > 3 else None
    if not cad:
        return False
    steps = cad * 2 if cad < 120 else cad
    return abs(w[0] - steps) < COUPLING_BPM


def lt1(sport_windows: list[dict], sport: str, ftp: float | None = None, bike_hr: float | None = None) -> dict:
    """LT1 from the pooled windows of one sport (each item: day, windows). Runs drop the windows
    locked to the step rate and are checked against the bike LT1 (`bike_hr`)."""
    acts = [a for a in sport_windows if a["windows"]]
    pts = [w for a in acts for w in a["windows"]]
    if sport == "run":
        pts = [w for w in pts if not coupled(w)]
    out = {"sport": sport, "activities": len(acts), "windows": len(pts), "hr": None, "watts": None,
           "since": min((a["day"] for a in acts), default=None), "status": "insufficient"}
    if len(acts) < MIN_ACTIVITIES:
        out["reason"] = f"{len(acts)} activities with chest-strap RR data in {LOOKBACK_DAYS} days (needs {MIN_ACTIVITIES})"
        return out
    hr, why = crossing([(w[0], w[1]) for w in pts], HR_BIN)
    if hr is None:
        out.update(status="unreliable" if "climbs back" in why else "insufficient", reason=why)
        return out
    if sport == "run" and bike_hr and hr < bike_hr - RUN_BELOW_BIKE_BPM:
        out.update(status="unreliable", estimate=round(hr), reason=(
            f"{hr:.0f} bpm is well below the bike LT1 ({bike_hr:.0f}) - heart rate locks to the step rate when "
            "running, so the run estimate isn't trusted"))
        return out
    out.update(hr=round(hr), status="ok", reason="ok")
    if sport == "ride":
        watts, why = crossing([(w[2], w[1]) for w in pts if w[2]], WATT_BIN)
        if watts and ftp and not LT1_PLAUSIBLE[0] <= watts / ftp <= LT1_PLAUSIBLE[1]:
            watts, why = None, f"LT1 power {watts:.0f} W is implausible for an FTP of {ftp:.0f} W"
        out["watts"] = round(watts) if watts else None
        if not watts:
            out["reason"] = f"power: {why}"
    return out


def z2_top(ftp: float | None, lt1_watts: float | None) -> float:
    """Top of Zone 2 as a share of FTP: 95% of LT1 power, within Z2_TOP_BOUNDS; 0.75 without LT1."""
    if not ftp or not lt1_watts:
        return Z2_TOP_DEFAULT
    lo, hi = Z2_TOP_BOUNDS
    return round(min(max(0.95 * lt1_watts / ftp, lo), hi), 3)


# --- critical speed ---

def _distance_at(distances: list[float], secs: list[int], t: int) -> float | None:
    """Best distance covered in `t` seconds, interpolated on a pace curve (distance -> best time)."""
    pairs = sorted(zip(secs, distances))
    for (s0, d0), (s1, d1) in zip(pairs, pairs[1:]):
        if s0 <= t <= s1 and s1 > s0:
            return d0 + (d1 - d0) * (t - s0) / (s1 - s0)
    return None


def critical_speed(d3: float | None, d9: float | None) -> dict | None:
    """{cs (m/s), d_prime (m)} from the best distances in 3 and 9 min, or None when implausible."""
    if not d3 or not d9:
        return None
    cs = (d9 - d3) / 360
    d_prime = d3 - cs * 180
    if not (CS_PLAUSIBLE[0] <= cs <= CS_PLAUSIBLE[1] and D_PRIME_PLAUSIBLE[0] <= d_prime <= D_PRIME_PLAUSIBLE[1]
            and d3 / 180 > d9 / 540):
        return None
    return {"cs": round(cs, 3), "d_prime": round(d_prime)}


def pace(speed: float) -> str:
    """m/s -> 'm:ss' per km."""
    sec = round(1000 / speed)
    return f"{sec // 60}:{sec % 60:02d}"


def _pace_curve(env: dict, curve: str) -> dict:
    r = requests.get(f"{API}/athlete/{env['INTERVALS_ATHLETE_ID']}/pace-curves.json",
                     params={"type": "Run", "curves": curve}, auth=("API_KEY", env["INTERVALS_API_KEY"]), timeout=60)
    r.raise_for_status()
    return (r.json().get("list") or [{}])[0]


def cs_status(env: dict, test_day: date | None, today: date) -> dict:
    """CS from the last CS test (source 'test'), else Intervals.icu's model of the last 90 days of
    training (source 'training', a floor: not used for paces)."""
    unusable = None
    if test_day and (today - test_day).days <= CS_TEST_DAYS:
        c = _pace_curve(env, f"r.{test_day}.{test_day}")
        d, s = c.get("distance") or [], c.get("values") or []
        res = critical_speed(_distance_at(d, s, 180), _distance_at(d, s, 540))
        if res:
            return {**res, "source": "test", "day": test_day.isoformat(), "pace": pace(res["cs"])}
        logger.warning(f"CS test on {test_day} gave no plausible critical speed")
        unusable = test_day.isoformat()
    model = next((m for m in _pace_curve(env, "90d").get("paceModels") or [] if m.get("type") == "CS"), None)
    if model and model.get("criticalSpeed"):
        cs = float(model["criticalSpeed"])
        return {"cs": round(cs, 3), "d_prime": round(model.get("dPrime") or 0), "source": "training",
                "pace": pace(cs), "day": None, "test_unusable": unusable}
    return {"cs": None, "source": None, "test_unusable": unusable}


# --- state ---

def _read(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _write(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _streams(env: dict, external_id: str) -> dict:
    r = requests.get(f"{API}/activity/{external_id}/streams.json", params={"types": "hrv,watts,cadence"},
                     auth=("API_KEY", env["INTERVALS_API_KEY"]), timeout=60)
    r.raise_for_status()
    return {s["type"]: s.get("data") or [] for s in r.json()}


def update(today: date | None = None) -> dict:
    """Fetches the RR streams of new activities, recomputes LT1 (ride, run) and CS, writes the state."""
    from core.overview import query
    from core.schedule import RUN_TEST_NAME, _targets
    today = today or date.today()
    env = load_env()
    rows = query("""SELECT external_id, type, name, start_time::date AS day FROM activities
                    WHERE superseded_by IS NULL AND type = ANY(%s) AND start_time::date >= %s
                    ORDER BY start_time;""", (list(RIDE_TYPES + RUN_TYPES), today - timedelta(days=LOOKBACK_DAYS)))
    cache = _read(WINDOWS_CACHE)
    if cache.get("_version") != CACHE_VERSION:
        cache = {"_version": CACHE_VERSION}
    for r in rows:
        key = str(r["external_id"])
        if key in cache:
            continue
        try:
            st = _streams(env, key)
            cache[key] = windows(st.get("hrv"), st.get("watts"), st.get("cadence"))
        except Exception as e:
            logger.warning(f"RR stream of {key} unavailable: {e}")
    keep = {str(r["external_id"]) for r in rows} | {"_version"}
    cache = {k: v for k, v in cache.items() if k in keep}
    _write(WINDOWS_CACHE, cache)

    t = _targets()
    by_sport = {"ride": [], "run": []}
    for r in rows:
        sport = "run" if r["type"] in RUN_TYPES else "ride"
        by_sport[sport].append({"day": r["day"].isoformat(), "windows": cache.get(str(r["external_id"])) or []})
    tests = [r["day"] for r in rows if r["type"] in RUN_TYPES and RUN_TEST_NAME.search(r["name"] or "")]
    try:
        cs = cs_status(env, tests[-1] if tests else t.get("last_run_test"), today)
    except Exception as e:
        logger.warning(f"Critical speed unavailable: {e}")
        cs = (_read(STATE).get("cs") or {"cs": None, "source": None})
    state = {"updated": datetime.now().isoformat(timespec="minutes"), "ftp": t.get("ftp"),
             "ride": lt1(by_sport["ride"], "ride", t.get("ftp")), "cs": cs}
    state["run"] = lt1(by_sport["run"], "run", bike_hr=state["ride"].get("hr"))
    indoor = [r for r in rows if r["type"] == "VirtualRide"]
    state["indoor_rr"] = {"with": sum(1 for r in indoor if cache.get(str(r["external_id"]))),
                          "without": sum(1 for r in indoor if not cache.get(str(r["external_id"]))),
                          "last_with": next((r["day"].isoformat() for r in reversed(indoor)
                                             if cache.get(str(r["external_id"]))), None),
                          "last_without": next((r["day"].isoformat() for r in reversed(indoor)
                                                if not cache.get(str(r["external_id"]))), None)}
    _write(STATE, state)
    return state


def current() -> dict:
    """The last update()'s result ({} before the first)."""
    from core.overview import cached
    return cached("thresholds", 300, lambda: _read(STATE))


def summary(state: dict | None = None, ftp: float | None = None) -> str:
    """One line for prompts and Telegram."""
    s = state if state is not None else current()
    if not s:
        return "Thresholds: not measured yet."
    parts = []
    ride, run, cs = s.get("ride") or {}, s.get("run") or {}, s.get("cs") or {}
    if ride.get("status") == "ok" and ride.get("watts"):
        top = z2_top(ftp or s.get("ftp"), ride["watts"])
        parts.append(f"bike LT1 {ride['hr']} bpm, {ride['watts']} W (DFA-a1, {ride['activities']} rides) -> "
                     f"Zone 2 up to {top * 100:.0f}% FTP")
    elif ride.get("status") == "ok":
        parts.append(f"bike LT1 {ride['hr']} bpm (DFA-a1, {ride['activities']} rides), its power not measured yet "
                     f"({ride['reason'].removeprefix('power: ')}) -> Zone 2 stays 56-75% FTP until the FTP test's "
                     "step warm-up fills it in")
    else:
        parts.append(f"bike LT1 not measured ({ride.get('reason', 'no data')}) -> Zone 2 at the classic 56-75% FTP")
    if run.get("status") == "ok":
        parts.append(f"run LT1 {run['hr']} bpm (easy runs stay under it)")
    else:
        parts.append(f"run LT1 not measured ({run.get('reason', 'no data')})")
    if cs.get("source") == "test":
        parts.append(f"critical speed {cs['pace']}/km (CS test {cs['day']}, D' {cs['d_prime']} m)")
    elif cs.get("cs"):
        parts.append(f"critical speed at least {cs['pace']}/km (from training runs - a CS test will set it)")
    return "Thresholds: " + "; ".join(parts) + "."


def changes(before: dict, after: dict) -> str | None:
    """A Telegram line when Zone 2 moved (LT1 power) or a new CS test set the run paces."""
    out = []
    ftp = after.get("ftp")
    old_top = z2_top(before.get("ftp") or ftp, (before.get("ride") or {}).get("watts"))
    new_top = z2_top(ftp, (after.get("ride") or {}).get("watts"))
    if abs(new_top - old_top) >= 0.02:
        out.append(f"Zone 2 now goes up to {new_top * 100:.0f}% FTP (was {old_top * 100:.0f}%): LT1 measured at "
                   f"{(after.get('ride') or {}).get('watts')} W from the beat-to-beat data.")
    rr_old, rr_new = before.get("indoor_rr") or {}, after.get("indoor_rr") or {}
    if rr_new.get("last_with") and rr_new["last_with"] != rr_old.get("last_with") and not rr_old.get("with"):
        out.append("Indoor rides now carry beat-to-beat (RR) data - they count towards LT1 and Zone 2 from now on.")
    cs_old, cs_new = before.get("cs") or {}, after.get("cs") or {}
    if cs_new.get("source") == "test" and cs_new.get("day") != cs_old.get("day"):
        out.append(f"CS test read: critical speed {cs_new['pace']}/km (D' {cs_new['d_prime']} m) - the sub-threshold "
                   "runs now come with paces.")
    if cs_new.get("test_unusable") and cs_new["test_unusable"] != cs_old.get("test_unusable"):
        out.append(f"The CS test on {cs_new['test_unusable']} gave no usable critical speed (the 3- and 9-min "
                   "efforts don't fit two all-out efforts: e.g. a paused watch, hills, or holding back). "
                   "Paces stay off; the test calendar plans the next one.")
    return ("🧪 " + "\n".join(out)) if out else None
