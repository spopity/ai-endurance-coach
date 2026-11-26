"""
Learning from the athlete's own data: a log of every adjustment the coach made, the markers
around it, and a quarterly review of what actually worked.

- log(kind, what, why): one line in state/adjustments.jsonl for each change the coach makes on
  its own - the plan's week adapted (phase / deload), a stimulus started or ended (core/progress),
  a threshold moved (Zone 2, critical speed), a new FTP, a goal status change, a low-energy flag.
- snapshot(): the markers of the day, one line a night in state/markers.jsonl (training FTP,
  eFTP, CTL, critical speed, the HM prediction, weight, 7-day HRV). The outcome of an adjustment is
  the change of these markers OUTCOME_DAYS later - measured, not guessed.
- review(): every quarter (first night of January, April, July, October; also /review in
  Telegram) - state/reviews/<year>-Q<n>.md and a Telegram message:
  1. the quarter's adjustments with what followed (eFTP / CTL / CS / HM pace 4 weeks later),
  2. what the stimuli of the progress check did (core/progress: improved / no change / worse),
  3. how much load the athlete absorbs: the weeks of the last year grouped by CTL ramp, with
     the HRV of the week after (vs its 4-week normal) - the highest ramp that kept HRV within
     ABSORB_HRV_PCT is the load the athlete handles; above it HRV drops,
  4. where the goals stand (core/goals).
"""
import json
import logging
import os
import statistics
from datetime import date, timedelta

logger = logging.getLogger("AI_COACH_V1.adjustments")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(ROOT, "state", "adjustments.jsonl")
MARKERS_FILE = os.path.join(ROOT, "state", "markers.jsonl")
REVIEW_DIR = os.path.join(ROOT, "state", "reviews")
OUTCOME_DAYS = 28
RAMP_BUCKETS = [(-99, 0, "falling"), (0, 3, "0-3/week"), (3, 5, "3-5/week"), (5, 7, "5-7/week"), (7, 99, "7+/week")]
ABSORB_HRV_PCT = -3.0
MIN_WEEKS = 3
KINDS = {"plan": "Plan", "stimulus": "Stimulus", "thresholds": "Thresholds", "ftp": "FTP",
         "goal": "Goal", "energy": "Energy", "review": "Weekly review"}


def _append(path: str, row: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _lines(path: str) -> list[dict]:
    try:
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return []


def log(kind: str, what: str, why: str = "", today: date | None = None, **extra):
    try:
        _append(LOG_FILE, {"day": (today or date.today()).isoformat(), "kind": kind, "what": what, "why": why, **extra})
    except OSError as e:
        logger.warning(f"Adjustment not logged: {e}")


def entries() -> list[dict]:
    return _lines(LOG_FILE)


def markers() -> dict[str, dict]:
    return {m["day"]: m for m in _lines(MARKERS_FILE)}


# --- the nightly snapshot ---

def snapshot(today: date | None = None) -> dict:
    """Today's markers; appended once a day. Also logs a new set FTP (compared with the last snapshot)."""
    from core.db import get_connection
    from core.metrics import load_athlete_profile
    from core.overview import query
    from core import goals, thresholds
    today = today or date.today()
    conn = get_connection()
    try:
        p = load_athlete_profile(conn)
    finally:
        conn.close()
    rows = query("SELECT day, ctl, hrv, weight_kg FROM daily_metrics WHERE day BETWEEN %s AND %s ORDER BY day;",
                 (today - timedelta(days=30), today))
    ctl = next((float(r["ctl"]) for r in reversed(rows) if r["ctl"] is not None), None)
    hrv7 = [float(r["hrv"]) for r in rows if r["hrv"] is not None and r["day"] > today - timedelta(days=7)]
    weights = [float(r["weight_kg"]) for r in rows if r["weight_kg"] is not None]
    cs = (thresholds.current() or {}).get("cs") or {}
    hm = (goals.current() or {}).get("half_marathon") or {}
    m = {"day": today.isoformat(), "ftp": p.get("ftp"), "ftp_set": p.get("ftp_set"), "ftp_source": p.get("ftp_source"),
         "eftp": round(float(p["eftp"]), 1) if p.get("eftp") else None, "ctl": round(ctl, 1) if ctl else None,
         "cs": cs.get("cs"), "cs_source": cs.get("source"), "hm_pace": hm.get("predicted_pace"),
         "weight": round(statistics.median(weights), 1) if weights else None,
         "hrv7": round(statistics.fmean(hrv7), 1) if hrv7 else None}
    known = markers()
    if today.isoformat() not in known:
        prev = known[max(known)] if known else None
        if prev and prev.get("ftp_set") and m["ftp_set"] and round(prev["ftp_set"]) != round(m["ftp_set"]):
            log("ftp", f"FTP set to {m['ftp_set']:.0f} W (was {prev['ftp_set']:.0f} W)", "new FTP in Intervals.icu",
                today)
        _append(MARKERS_FILE, m)
    return m


# --- the review ---

def outcome(entry_day: str, known: dict[str, dict], days: int = OUTCOME_DAYS) -> dict | None:
    """Marker changes from the adjustment's day to `days` later (nearest snapshots within 3 days)."""
    d0 = date.fromisoformat(entry_day)

    def near(d):
        return next((known[x.isoformat()] for x in (d + timedelta(days=i) for i in (0, 1, -1, 2, -2, 3, -3))
                     if x.isoformat() in known), None)
    a, b = near(d0), near(d0 + timedelta(days=days))
    if not a or not b:
        return None
    out = {}
    for k in ("eftp", "ftp", "ctl", "cs", "hm_pace", "weight"):
        if a.get(k) is not None and b.get(k) is not None:
            out[k] = round(b[k] - a[k], 3 if k == "cs" else 1)
    return out


def _fmt_outcome(o: dict | None) -> str:
    if o is None:
        return "too early to tell"
    parts = []
    if "eftp" in o:
        parts.append(f"eFTP {o['eftp']:+.0f} W")
    if "ctl" in o:
        parts.append(f"CTL {o['ctl']:+.0f}")
    if "hm_pace" in o:
        parts.append(f"HM pace {o['hm_pace']:+.0f} s/km")
    if "weight" in o:
        parts.append(f"weight {o['weight']:+.1f} kg")
    return ", ".join(parts) or "no markers"


def absorption(rows: list[dict]) -> list[dict]:
    """Weeks grouped by CTL ramp, with the next week's HRV vs its 4-week normal.
    `rows`: daily [{day, ctl, hrv}] oldest first."""
    by_day = {r["day"]: r for r in rows}
    if not rows:
        return []
    first, last = rows[0]["day"], rows[-1]["day"]
    monday = first + timedelta(days=(7 - first.weekday()) % 7 + 28)
    weeks = []
    while monday + timedelta(days=13) <= last:
        c0, c1 = (by_day.get(monday - timedelta(days=1)) or {}).get("ctl"), \
            (by_day.get(monday + timedelta(days=6)) or {}).get("ctl")
        nxt = [by_day[d]["hrv"] for d in (monday + timedelta(days=7 + i) for i in range(7))
               if d in by_day and by_day[d]["hrv"] is not None]
        base = [by_day[d]["hrv"] for d in (monday - timedelta(days=i) for i in range(1, 29))
                if d in by_day and by_day[d]["hrv"] is not None]
        if c0 is not None and c1 is not None and len(nxt) >= 4 and len(base) >= 14:
            weeks.append({"ramp": c1 - c0, "hrv_pct": (statistics.fmean(nxt) / statistics.fmean(base) - 1) * 100})
        monday += timedelta(days=7)
    out = []
    for lo, hi, label in RAMP_BUCKETS:
        w = [x for x in weeks if lo <= x["ramp"] < hi]
        if w:
            out.append({"ramp": label, "lo": lo, "weeks": len(w),
                        "hrv_pct": round(statistics.fmean(x["hrv_pct"] for x in w), 1),
                        "hrv_drops": sum(1 for x in w if x["hrv_pct"] <= -7)})
    return out


def absorbed_ramp(buckets: list[dict]) -> str | None:
    ok = [b for b in buckets if b["lo"] >= 0 and b["weeks"] >= MIN_WEEKS and b["hrv_pct"] >= ABSORB_HRV_PCT]
    return ok[-1]["ramp"] if ok else None


def quarter(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def review(today: date | None = None, stimuli: list[dict] | None = None, rows: list[dict] | None = None,
           goals_text: str | None = None) -> str:
    """The quarterly review (markdown) of the quarter that ended before `today` (or the running
    one when it is mid-quarter and called on demand)."""
    from core.overview import query
    today = today or date.today()
    y, q = quarter(today - timedelta(days=1))
    q_start = date(y, 3 * (q - 1) + 1, 1)
    known = markers()
    q_end = (date(y + 1, 1, 1) if q == 4 else date(y, 3 * q + 1, 1)) - timedelta(days=1)
    lines = [f"*Quarterly review {y}-Q{q}* ({q_start:%d %b} - {min(today - timedelta(days=1), q_end):%d %b})"]

    lines.append("\n*What the coach changed and what followed (4 weeks later)*")
    items = [e for e in entries() if q_start.isoformat() <= e["day"] < today.isoformat()]
    if not items:
        lines.append("No adjustments logged in this quarter.")
    for e in items:
        lines.append(f"- {e['day']} {KINDS.get(e['kind'], e['kind'])}: {e['what']} -> "
                     f"{_fmt_outcome(outcome(e['day'], known))}")

    if stimuli is None:
        try:
            from core.progress import _load_settings
            stimuli = _load_settings().get("history", [])
        except Exception:
            stimuli = []
    lines.append("\n*Stimuli of the progress check*")
    if stimuli:
        res: dict[str, list[str]] = {}
        for h in stimuli:
            res.setdefault(f"{h['sport']} {h['stimulus']}", []).append(str(h.get("result", "?")).replace("_", " "))
        lines += [f"- {k}: {', '.join(v)}" for k, v in res.items()]
    else:
        lines.append("None tried yet.")

    if rows is None:
        rows = query("SELECT day, ctl, hrv FROM daily_metrics WHERE day BETWEEN %s AND %s ORDER BY day;",
                     (today - timedelta(days=400), today))
        rows = [{"day": r["day"], "ctl": float(r["ctl"]) if r["ctl"] is not None else None,
                 "hrv": float(r["hrv"]) if r["hrv"] is not None else None} for r in rows]
    buckets = absorption(rows)
    lines.append("\n*Load you absorb (last 12 months: CTL ramp of a week -> HRV the week after)*")
    for b in buckets:
        lines.append(f"- ramp {b['ramp']}: {b['weeks']} weeks, HRV {b['hrv_pct']:+.1f}% vs normal"
                     + (f", {b['hrv_drops']} with a clear drop" if b["hrv_drops"] else ""))
    best = absorbed_ramp(buckets)
    lines.append(f"Highest ramp your HRV handles: CTL {best}." if best else
                 "Not enough weeks yet to say which ramp you absorb.")

    if goals_text is None:
        from core import goals
        goals_text = goals.markdown()
    if goals_text:
        lines.append("\n" + goals_text)
    return "\n".join(lines)


def quarterly(today: date | None = None) -> str | None:
    """Nightly: on the first night of a quarter, write the review of the one that ended; returns it."""
    today = today or date.today()
    y, q = quarter(today - timedelta(days=1))
    if quarter(today) == (y, q):
        return None
    path = os.path.join(REVIEW_DIR, f"{y}-Q{q}.md")
    if os.path.exists(path):
        return None
    text = review(today)
    os.makedirs(REVIEW_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write(text + "\n")
    return text
