"""
The athlete's measurable goals and whether the training is on the way there.

The goals live in state/goals.json (personal - never in the code or the public copy):

    {"ftp": {"target_w": 0, "above": true, "by": "YYYY-MM-DD", "start": "YYYY-MM-DD", "start_w": 0},
     "half_marathon": {"pace_sec_km": 0}}

FTP goal - a straight path from the FTP when the goal was set (start_w) to the target on the
date. The measured FTP follows the FTP rule (core/metrics.py): the tested FTP for 8 weeks,
then eFTP. A tested FTP is judged against the path on the day of the test (between tests the
path keeps rising while the tested value can't, which says nothing). Status:
- reached: at or over the target,
- ahead / on track / behind: the measured FTP vs the path, +-ON_TRACK_PCT,
- the gain still needed per week to the date, and Intervals.icu's eFTP as the day-to-day signal.

Half marathon - "able to run it at the target pace", kept, not one race day:
- predicted pace: Riegel (T2 = T1 * (21.1 / d1)^1.06) from the best efforts of the last 90 days
  (Intervals.icu pace curve, 3 km and longer), and from critical speed when a CS test measured
  it (HM ~ HM_CS_SHARE of CS, Smyth & Muniz-Pumares 2020) - the faster one counts, as training
  efforts are rarely all-out,
- endurance: the longest run of the last 6 weeks against ENDURANCE_SHARE of the race time at
  the target pace (the long run of the 7-day plan grows towards it),
- ready: pace within READY_SEC and the endurance there; close: within CLOSE_SEC; else building.

Nightly (scripts/update_plan.py): update() stores the status in state/goals_status.json and
returns a Telegram message when a status changes. The progress check, /progress, the coach and
the dashboard read it.
"""
import json
import logging
import os
from datetime import date, timedelta

logger = logging.getLogger("AI_COACH_V1.goals")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOALS_FILE = os.path.join(ROOT, "state", "goals.json")
STATUS_FILE = os.path.join(ROOT, "state", "goals_status.json")
HM_M = 21097.5
RIEGEL = 1.06
MIN_REF_M = 3000
HM_CS_SHARE = 0.93
ON_TRACK_PCT = 1.5
READY_SEC, CLOSE_SEC = 3, 15
ENDURANCE_SHARE = 0.85
LONG_RUN_DAYS = 42
STATUS_TEXT = {"reached": "✅ reached", "ahead": "ahead of the path", "on_track": "on track",
               "behind": "behind the path", "ready": "✅ ready", "close": "close", "building": "building"}


def load() -> dict:
    try:
        with open(GOALS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def mmss(sec: float) -> str:
    sec = round(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def hms(sec: float) -> str:
    sec = round(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# --- FTP ---

def ftp_status(goal: dict, measured: float | None, measured_day: date | None, eftp: float | None,
               today: date) -> dict | None:
    if not goal or not measured:
        return None
    start, by = date.fromisoformat(goal["start"]), date.fromisoformat(goal["by"])
    target = float(goal["target_w"]) + (1 if goal.get("above") else 0)
    start_w = float(goal.get("start_w") or measured)

    def path(d: date) -> float:
        f = min(1.0, max(0.0, (d - start).days / max(1, (by - start).days)))
        return start_w + (target - start_w) * f
    on = measured_day or today
    expected = path(on)
    gap_pct = (measured / expected - 1) * 100
    if measured >= target:
        status = "reached"
    elif gap_pct >= ON_TRACK_PCT:
        status = "ahead"
    elif gap_pct > -ON_TRACK_PCT:
        status = "on_track"
    else:
        status = "behind"
    weeks_left = max(0.0, (by - today).days / 7)
    per_week = (target - measured) / weeks_left if weeks_left and measured < target else 0.0
    label = f"FTP {'above ' + str(goal['target_w']) if goal.get('above') else goal['target_w']} W by {by:%d %b %Y}"
    text = (f"{label}: {STATUS_TEXT[status]} - {measured:.0f} W"
            + (f" (tested {on:%d %b})" if measured_day else " (eFTP)")
            + f" vs {expected:.0f} W on the path")
    if status != "reached":
        text += f"; needs +{per_week:.1f} W/week, {weeks_left:.0f} weeks left"
    if eftp and measured_day:
        text += f"; eFTP {eftp:.0f} W"
    return {"goal": label, "status": status, "measured": round(measured), "expected": round(expected),
            "target": round(target), "per_week": round(per_week, 1), "weeks_left": round(weeks_left),
            "eftp": round(eftp) if eftp else None, "text": text}


# --- half marathon ---

def time_at(distances: list[float], secs: list[float], d: float) -> float | None:
    """Best time over distance d on a pace curve (distance -> best time), interpolated."""
    pairs = sorted(zip(distances, secs))
    for (d0, s0), (d1, s1) in zip(pairs, pairs[1:]):
        if d0 <= d <= d1 and d1 > d0:
            return s0 + (s1 - s0) * (d - d0) / (d1 - d0)
    return None


def hm_prediction(distances: list[float], secs: list[float], cs: dict | None) -> dict | None:
    """{time, pace, source} - the fastest of Riegel from the best efforts and the CS estimate."""
    options = []
    pairs = [(d, s) for d, s in zip(distances, secs) if d >= MIN_REF_M and s]
    for ref in (3000, 5000, 10000, 15000, HM_M):
        t = time_at(distances, secs, ref)
        if t and pairs and ref <= max(d for d, _ in pairs):
            options.append((t * (HM_M / ref) ** RIEGEL, f"best {ref / 1000:g} km in {mmss(t)}"))
    if cs and cs.get("source") == "test" and cs.get("cs"):
        options.append((HM_M / (cs["cs"] * HM_CS_SHARE), f"critical speed {cs.get('pace')}/km"))
    if not options:
        return None
    t, src = min(options)
    return {"time": round(t), "pace": round(t / (HM_M / 1000)), "source": src}


def hm_status(goal: dict, prediction: dict | None, longest_run_min: float | None) -> dict | None:
    if not goal or not goal.get("pace_sec_km"):
        return None
    target = float(goal["pace_sec_km"])
    need_min = round(target * HM_M / 1000 / 60 * ENDURANCE_SHARE)
    label = f"Half marathon at {mmss(target)}/km ({hms(target * HM_M / 1000)})"
    if not prediction:
        return {"goal": label, "status": "building", "text": f"{label}: no recent run data to predict it"}
    gap = prediction["pace"] - target
    endurance = bool(longest_run_min and longest_run_min >= need_min)
    status = "ready" if gap <= READY_SEC and endurance else "close" if gap <= CLOSE_SEC else "building"
    long_txt = f"longest run {longest_run_min:.0f} min" if longest_run_min else "no long run"
    text = (f"{label}: {STATUS_TEXT[status]} - predicted {mmss(prediction['pace'])}/km "
            f"({hms(prediction['time'])}, from {prediction['source']})"
            + (f", {gap:+.0f} s/km to go" if gap > 0 else "")
            + f"; endurance: {long_txt} of the 6 weeks" + (" ✓" if endurance else f", needs ~{need_min} min"))
    return {"goal": label, "status": status, "predicted_pace": prediction["pace"], "predicted_time": prediction["time"],
            "gap_sec_km": round(gap), "longest_run_min": round(longest_run_min) if longest_run_min else None,
            "endurance_ok": endurance, "need_min": need_min, "text": text}


# --- nightly + readers ---

def compute(today: date | None = None) -> dict:
    from core.db import get_connection, load_env
    from core.metrics import load_athlete_profile
    from core.overview import query
    from core import thresholds
    today = today or date.today()
    goals = load()
    out = {"today": today.isoformat(), "ftp": None, "half_marathon": None}
    if goals.get("ftp"):
        conn = get_connection()
        try:
            p = load_athlete_profile(conn)
        finally:
            conn.close()
        tested = p.get("ftp_source") == "set" and p.get("ftp_updated")
        out["ftp"] = ftp_status(goals["ftp"], p.get("ftp"),
                                date.fromisoformat(p["ftp_updated"]) if tested else None, p.get("eftp"), today)
    if goals.get("half_marathon"):
        env = load_env()
        c = thresholds._pace_curve(env, "90d")
        pred = hm_prediction(c.get("distance") or [], c.get("values") or [], thresholds.current().get("cs"))
        rows = query("""SELECT max(moving_time_sec) / 60.0 AS m FROM activities WHERE superseded_by IS NULL
                        AND type IN ('Run', 'TrailRun', 'VirtualRun') AND start_time >= %s;""",
                     (today - timedelta(days=LONG_RUN_DAYS),))
        out["half_marathon"] = hm_status(goals["half_marathon"], pred, float(rows[0]["m"]) if rows and rows[0]["m"] else None)
    return out


def _read_status() -> dict:
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def changes(before: dict, after: dict) -> str | None:
    lines = []
    for key in ("ftp", "half_marathon"):
        b, a = (before.get(key) or {}).get("status"), (after.get(key) or {}).get("status")
        if a and b and a != b:
            lines.append(f"🎯 {after[key]['text']} (was {STATUS_TEXT.get(b, b)})")
    return "\n".join(lines) or None


def update(today: date | None = None) -> tuple[dict, str | None]:
    """Nightly: (status, Telegram message when a goal's status changed)."""
    before = _read_status()
    now = compute(today)
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(now, f, indent=1)
    os.replace(tmp, STATUS_FILE)
    return now, changes(before, now)


def current() -> dict:
    return _read_status()


def summary(status: dict | None = None) -> str:
    st = current() if status is None else status
    lines = [g["text"] for g in (st.get("ftp"), st.get("half_marathon")) if g]
    return ("Goals: " + "; ".join(lines) + ".") if lines else "Goals: none set."


def markdown(status: dict | None = None) -> str:
    st = current() if status is None else status
    lines = [f"🎯 {g['text']}" for g in (st.get("ftp"), st.get("half_marathon")) if g]
    return ("*Goals*\n" + "\n".join(lines)) if lines else ""
