"""
The progress check: is the training making the athlete faster, and if not, what changes?

The plan (core/periodization.py) ramps the load and readiness (core/readiness.py) checks it is
absorbed; this closes the loop on the OUTPUT. All numbers come from code:

Markers, each per 28-day window (the last four windows, so trends over 4 and 8 weeks):
- bike: best 5-min and 20-min power (Intervals.icu power curves), eFTP (Intervals.icu rolling
  eFTP at the end of the window), aerobic efficiency (median watts per heartbeat in Zone 2,
  Intervals' power:HR Z2, on rides with 20+ min of Zone 2),
- durability (core/durability.py): heart-rate drift on steady long rides (lower is better) and
  the best 20-min power after 15 kJ/kg as a share of the FTP of that day,
- run: running economy at easy effort (median metres per heartbeat, grade-adjusted, on easy runs
  of 25+ min).

Each marker is up / flat / down against the window before it (more than its smallest worthwhile
change, SWC), or up when it climbed slowly over 8 weeks. Best-effort power can only be
"down" when the window had a session that would have shown it (a VO2max session for 5-min power,
threshold work or a test for 20-min power and eFTP): no hard effort means "untested", not slower.

The verdict per sport: improving / flat / declining / mixed, and a diagnosis with the cause:
- improving (more markers up than down) -> keep the plan,
- after an illness in the last 4 weeks: the dip is the illness - judged again later,
- fatigued (red readiness): the plan's recovery comes first,
- inconsistent (fewer than 2 of the last 3 loading weeks completed): the load is the limiter, not the
  session type - no change of stimulus,
- stalled (not improving although the weeks were completed): change the stimulus for
  EMPHASIS_DAYS. Bike in Build: more threshold, then a VO2max block (one sweet-spot session a week
  becomes VO2max), then volume; in Base only volume (a longer long ride). Run: rotate the quality
  run's type. A stimulus tried in the last RETRY_AFTER_DAYS isn't tried again first.

The stimulus never changes the plan's phase, load targets or session mix (training_blocks); it
only changes which ladder a quality session uses (core/schedule.bike_type / run_type) and the
long ride's growth. When it ends, it is judged: did the stalled markers move?

update() runs nightly (scripts/update_plan.py) and writes state/progress.json; report() and
emphasis() read it for the coach, the dashboard and Telegram.
"""
import json
import logging
import os
import statistics
from datetime import date, datetime, timedelta

import requests

from core import durability, progression
from core.db import ROOT, get_connection, load_env

logger = logging.getLogger("AI_COACH_V1.progress")

STATE_FILE = os.path.join(ROOT, "state", "progress.json")
WINDOW_DAYS = 28
WINDOWS = 4
EMPHASIS_DAYS = 21
RETRY_AFTER_DAYS = 84
HISTORY_KEPT = 20
MIN_SAMPLES = 2          # sessions a median marker needs in a window
MIN_Z2_MINUTES = 20
EASY_RUN_MIN = 25
EASY_RUN_SHARE = 0.75    # share of an easy run's time in HR Z1-2

# name: (label, sport, smallest worthwhile change, unit, +1 higher is better / -1 lower is better,
#        True when the change is absolute (percentage points) rather than a share)
MARKERS = {
    "eftp": ("eFTP", "ride", 0.015, "W", 1, False),
    "p20": ("best 20-min power", "ride", 0.02, "W", 1, False),
    "p5": ("best 5-min power", "ride", 0.02, "W", 1, False),
    "aerobic": ("aerobic efficiency (W per beat in Zone 2)", "ride", 0.02, "", 1, False),
    "decoupling": ("heart-rate drift on steady long rides", "ride", 1.0, "%", -1, True),
    "late_power": ("best 20-min power after 15 kJ/kg, share of FTP", "ride", 0.02, "share", 1, True),
    "run_economy": ("easy-run economy (m per heartbeat)", "run", 0.02, "m", 1, False),
}


def change(name: str, old: float, new: float) -> float:
    """The improvement from old to new in the marker's own terms (positive = better)."""
    _, _, _, _, better, absolute = MARKERS[name]
    return better * ((new - old) if absolute else (new - old) / old)
# A best-effort marker only counts as down when the window had a session that tests it.
TESTED_BY = {"p5": {"vo2", "test"}, "p20": {"threshold", "sweet_spot", "vo2", "test"},
             "eftp": {"threshold", "sweet_spot", "vo2", "test"}}
BIKE_STIMULI = ("threshold", "vo2", "volume")
RUN_STIMULI = ("run_threshold", "run_hills", "run_tempo")
STIMULUS_TEXT = {
    "threshold": "more time at threshold: sweet-spot sessions become threshold intervals",
    "vo2": "a VO2max block: one quality ride a week becomes VO2max intervals, to raise the ceiling",
    "volume": "more aerobic volume: the long ride grows by 30 min a week instead of 15",
    "run_threshold": "threshold intervals as the quality run",
    "run_hills": "hill repeats as the quality run (strength and power)",
    "run_tempo": "longer tempo intervals as the quality run",
}


# --- windows and markers ---

def windows(today: date) -> list[tuple[date, date]]:
    """[(start, end)] of the last WINDOWS 28-day windows, newest first."""
    return [(today - timedelta(days=WINDOW_DAYS * (i + 1) - 1), today - timedelta(days=WINDOW_DAYS * i))
            for i in range(WINDOWS)]


def _api_power_bests(wins: list[tuple[date, date]]) -> list[dict]:
    env = load_env()
    curves = ",".join(f"r.{a}.{b}" for a, b in wins)
    r = requests.get(f"https://intervals.icu/api/v1/athlete/{env['INTERVALS_ATHLETE_ID']}/power-curves.json",
                     params={"type": "Ride", "curves": curves}, auth=("API_KEY", env["INTERVALS_API_KEY"]), timeout=60)
    r.raise_for_status()
    out = []
    for c in r.json().get("list", []):
        secs, watts = c.get("secs") or [], c.get("watts") or []
        best = dict(zip(secs, watts))
        out.append({"p5": best.get(300), "p20": best.get(1200)})
    return out


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if len(values) >= MIN_SAMPLES else None


def _local_markers(conn, wins: list[tuple[date, date]], fetch: bool = True) -> list[dict]:
    """eFTP, aerobic efficiency, heart-rate drift, late power, easy-run economy and the tested
    session types per window. `fetch`: download missing power streams for late power."""
    from core.metrics import FTP_TEST_NAME
    from core.plan import RIDE_TYPES, RUN_TYPES
    from core.schedule import RUN_TEST_NAME, TZ
    with conn.cursor() as cur:
        cur.execute("""
            SELECT (start_time AT TIME ZONE %s)::date AS day, type, name, moving_time_sec, avg_hr, ftp_at_time,
                   external_id, decoupling_pct, variability_index, intensity_factor,
                   (raw_payload->>'icu_joules')::float AS joules,
                   (raw_payload->>'icu_rolling_ftp')::float AS eftp,
                   (raw_payload->>'icu_power_hr_z2')::float AS phz2,
                   (raw_payload->>'icu_power_hr_z2_mins')::float AS phz2_mins,
                   COALESCE((raw_payload->>'gap')::float, (raw_payload->>'pace')::float, average_speed_mps) AS speed,
                   raw_payload->'icu_hr_zone_times' AS hz, raw_payload->'interval_summary' AS summary
            FROM activities WHERE superseded_by IS NULL AND type = ANY(%s) AND start_time >= %s
            ORDER BY start_time;
        """, (str(TZ), list(RIDE_TYPES | RUN_TYPES), wins[-1][0] - timedelta(days=progression.FTP_LOOKBACK_DAYS)))
        rows = cur.fetchall()
    ftps = progression.training_ftps(rows)
    from core.metrics import load_athlete_profile
    weight = load_athlete_profile(conn).get("weight_kg")
    recent = [r for r in rows if r["type"] in RIDE_TYPES and r["day"] >= wins[-1][0]]
    late = durability.late_power(recent, float(weight) if weight else None, fetch=fetch)
    out = []
    for start, end in wins:
        w = [r for r in rows if start <= r["day"] <= end]
        rides = [r for r in w if r["type"] in RIDE_TYPES]
        eftp = next((r["eftp"] for r in reversed(rides) if r["eftp"]), None)
        aerobic = _median([r["phz2"] for r in rides if r["phz2"] and (r["phz2_mins"] or 0) >= MIN_Z2_MINUTES])
        economy = []
        for r in w:
            hz = [s or 0 for s in (r["hz"] or [])]
            if (r["type"] in RUN_TYPES and r["avg_hr"] and r["speed"] and sum(hz)
                    and (r["moving_time_sec"] or 0) >= EASY_RUN_MIN * 60 and sum(hz[:2]) >= EASY_RUN_SHARE * sum(hz)):
                economy.append(r["speed"] * 60 / r["avg_hr"])
        tested = set()
        for r in rides:
            if FTP_TEST_NAME.search(r["name"] or ""):
                tested.add("test")
            tested |= {t for t, secs in progression.ride_work(r["summary"], ftps.get(id(r))).items()
                       if secs >= progression.MIN_SHARE * progression.work(t, 0)}
        if any(r["type"] in RUN_TYPES and RUN_TEST_NAME.search(r["name"] or "") for r in w):
            tested.add("run_test")
        drift = durability.median([dec for _, dec in durability.steady_decoupling(rides)])
        shares = [durability.late_share(late.get(r["external_id"]), ftps.get(id(r))) for r in rides]
        shares = [x for x in shares if x]
        out.append({"eftp": eftp, "aerobic": round(aerobic, 3) if aerobic else None,
                    "decoupling": drift, "late_power": max(shares) if shares else None,
                    "run_economy": round(_median(economy), 3) if _median(economy) else None,
                    "tested": sorted(tested)})
    return out


# --- judging ---

def trend(name: str, values: list, tested: list[set]) -> str | None:
    """'up' (better) / 'flat' / 'down' (worse) / 'untested' for values newest first (None when
    not enough data). For heart-rate drift 'up' means less drift."""
    swc = MARKERS[name][2]
    a, b = values[0], values[1] if len(values) > 1 else None
    if a is None or b is None:
        return None
    d = change(name, b, a)
    if d >= swc:
        return "up"
    c = values[2] if len(values) > 2 else None
    if d <= -swc:
        if name in TESTED_BY and not (TESTED_BY[name] & tested[0]):
            return "untested"
        return "down"
    if c is not None and change(name, c, a) >= 1.5 * swc:
        return "up"   # a slow climb over 8 weeks
    return "flat"


def verdict(trends: dict[str, str | None]) -> str:
    t = [v for v in trends.values() if v in ("up", "flat", "down")]
    if not t:
        return "no_data"
    up, down = t.count("up"), t.count("down")
    if up and down:   # more markers moving one way decide it; a tie is mixed
        return "improving" if up > down else "declining" if down > up else "mixed"
    return "improving" if up else "declining" if down else "flat"


def _consistency(conn, today: date) -> dict:
    """Completed loading weeks among the last 3 closed loading weeks (core/periodization verdicts)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT start_date, status FROM training_blocks
                       WHERE end_date < %s AND NOT is_deload AND status IS NOT NULL
                       ORDER BY start_date DESC LIMIT 3;""", (today - timedelta(days=today.weekday()),))
        rows = cur.fetchall()
    done = sum(1 for r in rows if r["status"] == "completed")
    return {"completed": done, "of": len(rows), "consistent": len(rows) >= 2 and done >= 2}


def diagnose(sport: str, now: str, before: str, consistency: dict, readiness: str | None, phase: str | None,
             trends: dict, history: list[dict], today: date, illness_end: date | None = None) -> dict:
    """What the verdict means and the stimulus to try (None = keep the plan)."""
    if now == "no_data":
        return {"diagnosis": "no_data", "why": "not enough sessions with data in the last 8 weeks", "stimulus": None}
    if now == "improving":
        return {"diagnosis": "progressing", "why": "markers are moving up - keep the plan", "stimulus": None}
    if now == "mixed":
        return {"diagnosis": "progressing", "stimulus": None,
                "why": "some markers up, some down - progress, but watch the falling ones"}
    if illness_end and (today - illness_end).days < WINDOW_DAYS:
        return {"diagnosis": "after_illness", "stimulus": None,
                "why": (f"ill until {illness_end} - markers dip after an illness and come back with training; "
                        "judged again once a full 4-week window has passed")}
    if readiness == "red":
        return {"diagnosis": "fatigued", "why": "readiness is red - recovery comes before a new stimulus",
                "stimulus": None}
    if not consistency["consistent"]:
        return {"diagnosis": "inconsistent",
                "why": (f"only {consistency['completed']} of the last {consistency['of']} loading weeks were "
                        "completed - doing the planned load is the lever, not a different session"),
                "stimulus": None}
    stalled_for = 8 if before not in ("improving", None, "no_data") else 4
    recent = {h["stimulus"] for h in history
              if h["sport"] == sport and date.fromisoformat(h["started"]) >= today - timedelta(days=RETRY_AFTER_DAYS)}
    if sport == "ride":
        if phase not in ("Build", "Race prep"):
            options = ["volume"]
        elif trends.get("p5") in ("flat", "down") and trends.get("p20") == "up":
            options = ["vo2", "volume"]
        else:
            options = list(BIKE_STIMULI)
    else:
        options = list(RUN_STIMULI) if phase in ("Build", "Race prep") else []
    fresh = [o for o in options if o not in recent]
    stimulus = (fresh or options or [None])[0]
    return {"diagnosis": "stalled", "stalled_weeks": stalled_for, "stimulus": stimulus,
            "why": (f"no improvement for {stalled_for} weeks although {consistency['completed']} of the last "
                    f"{consistency['of']} loading weeks were completed - change the stimulus"
                    + (f": {STIMULUS_TEXT[stimulus]}" if stimulus else " (no quality-run options in this phase)"))}


# --- state ---

def _load_settings() -> dict:
    from core.overview import query
    rows = query("SELECT value FROM athlete_settings WHERE key = 'progress';")
    value = (rows[0]["value"] if rows else None) or {}
    return {"active": value.get("active") or {}, "history": value.get("history") or []}


def _save_settings(state: dict):
    state["history"] = state["history"][-HISTORY_KEPT:]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO athlete_settings (key, value, updated_at) VALUES ('progress', %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
            """, (json.dumps(state),))
        conn.commit()
    finally:
        conn.close()


def emphasis(today: date | None = None) -> dict:
    """The active stimulus per sport, e.g. {"bike": "vo2"} - read by core/schedule.py."""
    today = today or date.today()
    out = {}
    for sport, e in _load_settings()["active"].items():
        if e and date.fromisoformat(e["until"]) >= today:
            out["bike" if sport == "ride" else "run"] = e["stimulus"]
    return out


def report() -> dict | None:
    """The last nightly progress report (state/progress.json)."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# --- the nightly update ---

def compute(today: date | None = None, fetch: bool = True) -> dict:
    """The progress report (no side effects except reading the database and Intervals.icu, and
    caching power streams)."""
    today = today or date.today()
    wins = windows(today)
    conn = get_connection()
    try:
        local = _local_markers(conn, wins, fetch)
        consistency = _consistency(conn, today)
        with conn.cursor() as cur:
            cur.execute("SELECT phase FROM training_blocks WHERE start_date <= %s AND end_date >= %s;", (today, today))
            row = cur.fetchone()
        phase = row["phase"] if row else None
    finally:
        conn.close()
    try:
        bests = _api_power_bests(wins)
    except Exception as e:
        logger.warning(f"Power curves unavailable: {e}")
        bests = [{"p5": None, "p20": None}] * len(wins)
    rows = [{**b, **l} for b, l in zip(bests, local)]
    try:
        from core.readiness import current_readiness
        readiness = current_readiness().get("level")
    except Exception as e:
        logger.warning(f"Readiness unavailable for the progress check: {e}")
        readiness = None
    try:
        from core import availability
        data = availability.load()
        # last_illness_end looks back 2 weeks; asking again from 2 weeks ago covers the whole window.
        ends = [availability.last_illness_end(d, data) for d in (today, today - timedelta(days=14))]
        illness_end = max((e for e in ends if e), default=None)
    except Exception as e:
        logger.warning(f"Availability unavailable for the progress check: {e}")
        illness_end = None
    settings = _load_settings()
    tested = [set(r["tested"]) for r in rows]

    sports = {}
    for sport in ("ride", "run"):
        names = [n for n, m in MARKERS.items() if m[1] == sport]
        now = {n: trend(n, [r[n] for r in rows], tested) for n in names}
        before = {n: trend(n, [r[n] for r in rows[1:]], tested[1:]) for n in names}
        v_now, v_before = verdict(now), verdict(before)
        d = diagnose(sport, v_now, v_before, consistency, readiness, phase, now, settings["history"], today,
                     illness_end)
        sports[sport] = {"verdict": v_now, "verdict_4_weeks_ago": v_before, "trends": now, **d}
    return {
        "generated": datetime.now().isoformat(timespec="minutes"),
        "today": today.isoformat(),
        "phase": phase,
        "readiness": readiness,
        "consistency": consistency,
        "windows": [{"start": a.isoformat(), "end": b.isoformat(), **r} for (a, b), r in zip(wins, rows)],
        "sports": sports,
    }


def _judge(e: dict, rep: dict) -> str:
    """Did the stalled markers move while the stimulus ran? Compares today's window with the
    window that ended when it started."""
    start = date.fromisoformat(e["started"])
    sport = rep["sports"][e["sport"]]
    if sport["verdict"] == "improving":
        return "helped"
    newest = rep["windows"][0]
    base = e.get("markers_at_start") or {}
    moved = [n for n, v in base.items() if v and newest.get(n) and change(n, v, newest[n]) >= MARKERS[n][2]]
    if moved:
        return "helped"
    return "no_change" if (date.fromisoformat(rep["today"]) - start).days >= EMPHASIS_DAYS else "too_early"


def update(today: date | None = None) -> dict:
    """Nightly: compute the report, end / judge expired stimuli, start a new one where a sport
    stalled. Returns {report, started: [...], ended: [...]}."""
    today = today or date.today()
    rep = compute(today)
    state = _load_settings()
    started, ended = [], []
    for sport, info in rep["sports"].items():
        active = state["active"].get(sport)
        if active and date.fromisoformat(active["until"]) < today:
            result = _judge(active, rep)
            state["history"].append({**active, "ended": today.isoformat(), "result": result})
            ended.append({**active, "result": result})
            state["active"][sport] = active = None
        if not active and info["diagnosis"] == "stalled" and info.get("stimulus"):
            e = {"sport": sport, "stimulus": info["stimulus"], "started": today.isoformat(),
                 "until": (today + timedelta(days=EMPHASIS_DAYS - 1)).isoformat(), "why": info["why"],
                 "markers_at_start": {n: rep["windows"][0].get(n) for n, m in MARKERS.items() if m[1] == sport}}
            state["active"][sport] = e
            started.append(e)
    rep["emphasis"] = {s: e for s, e in state["active"].items() if e}
    rep["emphasis_history"] = state["history"][-6:]
    _save_settings(state)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rep, f, indent=1, default=str)
    os.replace(tmp, STATE_FILE)
    return {"report": rep, "started": started, "ended": ended}


# --- for people and the coach ---

ARROW = {"up": "↑", "flat": "→", "down": "↓", "untested": "?", None: "–"}


def _fmt(name: str, v) -> str:
    if v is None:
        return "–"
    unit = MARKERS[name][3]
    return (f"{v:.0f} W" if unit == "W" else f"{v:.1f}%" if unit == "%" else f"{v * 100:.0f}%" if unit == "share"
            else f"{v:.2f}")


def markdown(rep: dict | None = None) -> str:
    rep = rep or report()
    if not rep:
        return "No progress report yet - it is computed every night."
    lines = [f"*Progress check* ({rep['today']}, 4-week windows)"]
    w0, w1 = rep["windows"][0], rep["windows"][1]
    for sport, title in (("ride", "🚴 Bike"), ("run", "🏃 Run")):
        s = rep["sports"][sport]
        lines.append(f"\n*{title}: {s['verdict'].replace('_', ' ')}*")
        for n, (label, sp, *_) in MARKERS.items():
            if sp == sport and (w0.get(n) is not None or w1.get(n) is not None):
                lines.append(f"{ARROW[s['trends'].get(n)]} {label}: {_fmt(n, w0.get(n))} (was {_fmt(n, w1.get(n))})")
        lines.append(s["why"])
        e = (rep.get("emphasis") or {}).get(sport)
        if e:
            lines.append(f"Stimulus until {e['until']}: {STIMULUS_TEXT[e['stimulus']]}")
    return "\n".join(lines)


def context() -> dict:
    """For the coach's context: the verdicts, diagnosis and active stimulus per sport."""
    rep = report()
    if not rep:
        return {}
    w0, w1 = rep["windows"][0], rep["windows"][1]
    return {
        "as_of": rep["today"],
        "sports": {sport: {
            "verdict": s["verdict"], "diagnosis": s["diagnosis"], "why": s["why"],
            "markers": {MARKERS[n][0]: {"now": w0.get(n), "4_weeks_before": w1.get(n), "trend": t}
                        for n, t in s["trends"].items() if w0.get(n) is not None or w1.get(n) is not None},
            "stimulus": ((rep.get("emphasis") or {}).get(sport) or {}).get("stimulus"),
        } for sport, s in rep["sports"].items()},
        "stimulus_results": [{"sport": h["sport"], "stimulus": h["stimulus"], "result": h["result"]}
                             for h in rep.get("emphasis_history") or []],
    }
