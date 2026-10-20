"""
Adaptive periodization: the phase of each week is earned from what was actually
trained, not read off a calendar fixed in advance.

The engine replays every closed week since REPLAY_FROM, judges it against the
target it had (completed / partial / missed / recovery) and moves a small state
machine:

- Base comes first. Build only unlocks after BASE_WEEKS_REQUIRED *completed*
  base weeks. A partial week is repeated (doesn't count); a missed week costs one
  base week.
- MISSED_STREAK_RESET missed weeks in a row, or fitness (CTL) falling
  CTL_DROP_RESET below its peak in the current phase, resets to Base week 1 -
  wherever you were.
- A recovery week follows LOAD_WEEKS_BEFORE_RECOVERY loading weeks (earlier when
  form is very negative). A missed week already acts as rest, so it resets that
  count instead of being followed by a pointless deload.
- Weekly load targets come from the current CTL (the load that holds fitness is
  ~7 x CTL) plus a phase-specific CTL ramp, so they rescale automatically after a
  break instead of asking for the load of a fitter past self.

The result is written to training_blocks (past weeks with their verdict, this
week, and a projection of the coming weeks assuming they get completed), so the
dashboard, briefing and coach read it as before. Re-running is idempotent; the
current week only depends on closed weeks, so it doesn't change mid-week.
"""
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger("AI_COACH_V1.periodization")

REPLAY_FROM = date(2026, 1, 5)       # fixed anchor, so the replay is deterministic
BASE_WEEKS_REQUIRED = 6              # completed base weeks before Build unlocks
BASE_EARLY_WEEKS = 3                 # first base weeks: pure aerobic, no tempo
LOAD_WEEKS_BEFORE_RECOVERY = 3
RECOVERY_TSB = -25                   # recovery comes early after 2+ loading weeks below this form
MISSED_STREAK_RESET = 2
CTL_DROP_RESET = 0.25                # CTL 25% below the phase peak -> back to Base

# Planned CTL rise per week; weekly TSS to get it is ~ 7*CTL + 45*ramp
# (Banister, 42-day CTL: one week of constant daily load L moves CTL by ~0.154*(L - CTL)).
RAMP = {"Base": 2.0, "Build": 3.0}
CTL_GAIN_PER_WEEK = 1 - math.exp(-7 / 42)
MIN_TARGET = 120                     # loading-week floor (TSS) so a near-zero CTL still gets a real week

# Verdicts for loading weeks, relative to the week's target minimum.
COMPLETE_SHARE, MISSED_SHARE = 0.85, 0.5
MIN_TRAINING_DAYS = 3

FOCUS = {
    "base_early": "Base - aerobic foundation: Zone 2 endurance, cadence work and strides only, no intervals. "
                  "Consistency matters more than any single session.",
    "base_late": "Base - aerobic + tempo: mostly Zone 2, one tempo / low sweet spot session, no VO2max.",
    "build_1": "Build - threshold development: sweet spot and FTP-building intervals.",
    "build_2": "Build - threshold progression: longer sweet spot / threshold intervals.",
    "build_3": "Build - peak load week: VO2max/anaerobic work at the top of this block before recovery.",
    "recovery": "Deload - reduced volume and intensity to absorb the last weeks. No high-intensity intervals this week.",
}
# Planned session mix (cycling focus - dual mode adds runs on top, see core/focus.py).
SESSIONS = {
    "base_early": {"endurance": 4, "sst": 0, "vo2": 0, "run": 1, "weights": 3},
    "base_late": {"endurance": 3, "sst": 1, "vo2": 0, "run": 1, "weights": 4},
    "build_1": {"endurance": 3, "sst": 1, "vo2": 0, "run": 1, "weights": 4},
    "build_2": {"endurance": 2, "sst": 2, "vo2": 0, "run": 1, "weights": 4},
    "build_3": {"endurance": 2, "sst": 1, "vo2": 1, "run": 1, "weights": 3},
    "recovery": {"endurance": 3, "sst": 0, "vo2": 0, "run": 1, "weights": 3},
}


@dataclass
class State:
    phase: str = "Base"          # 'Base' | 'Build'
    progress: int = 0            # completed weeks in this phase
    load_weeks: int = 0          # loading weeks since the last rest
    missed_streak: int = 0
    ctl_peak: float = 0.0        # highest CTL seen in this phase
    events: list = field(default_factory=list)


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_targets(kind: str, ctl: float) -> tuple[float, float]:
    hold = 7 * ctl
    if kind == "recovery":
        return round(max(hold * 0.5, 60)), round(max(hold * 0.7, 90))
    phase = "Base" if kind.startswith("base") else "Build"
    center = max(hold + 7 * RAMP[phase] / CTL_GAIN_PER_WEEK, MIN_TARGET)
    return round(center * 0.9), round(center * 1.1)


def week_kind(s: State, tsb: float | None) -> str:
    if s.load_weeks >= LOAD_WEEKS_BEFORE_RECOVERY or (
            s.load_weeks >= 2 and tsb is not None and tsb < RECOVERY_TSB):
        return "recovery"
    if s.phase == "Base":
        return "base_early" if s.progress < BASE_EARLY_WEEKS else "base_late"
    return f"build_{min(s.load_weeks, 2) + 1}"


def judge(kind: str, load: float, days: int, lo: float, hi: float) -> str:
    if kind == "recovery":
        return "recovery"
    if load >= COMPLETE_SHARE * lo and days >= MIN_TRAINING_DAYS:
        return "completed"
    if load < MISSED_SHARE * lo or days <= 1:
        return "missed"
    return "partial"


def advance(s: State, kind: str, verdict: str, load: float, hi: float, ctl_end: float) -> str:
    """Moves the state past a closed week; returns why the next week is what it is."""
    s.ctl_peak = max(s.ctl_peak, ctl_end)
    if verdict == "recovery":
        # A "recovery" week trained well above its cap wasn't rest.
        s.load_weeks = 1 if load > hi * 1.3 else 0
        s.missed_streak = 0
    elif verdict == "completed":
        s.progress += 1
        s.load_weeks += 1
        s.missed_streak = 0
    elif verdict == "partial":
        s.load_weeks += 1
        s.missed_streak = 0
    else:  # missed - counts as rest, costs a week of progress
        s.missed_streak += 1
        s.load_weeks = 0
        s.progress = max(0, s.progress - 1)

    if s.missed_streak >= MISSED_STREAK_RESET:
        reason = f"{s.missed_streak} missed weeks in a row - reset to Base week 1"
        s.phase, s.progress, s.ctl_peak = "Base", 0, ctl_end
        return reason
    if s.ctl_peak and ctl_end < s.ctl_peak * (1 - CTL_DROP_RESET):
        reason = (f"fitness dropped from CTL {s.ctl_peak:.0f} to {ctl_end:.0f} "
                  f"(>{CTL_DROP_RESET:.0%}) - reset to Base week 1")
        s.phase, s.progress, s.ctl_peak, s.load_weeks = "Base", 0, ctl_end, 0
        return reason
    if s.phase == "Base" and s.progress >= BASE_WEEKS_REQUIRED:
        s.phase, s.progress, s.ctl_peak = "Build", 0, ctl_end
        return f"{BASE_WEEKS_REQUIRED} base weeks completed - Build unlocked"

    if s.phase == "Base":
        left = BASE_WEEKS_REQUIRED - s.progress
        return {"completed": f"base week completed ({s.progress}/{BASE_WEEKS_REQUIRED}); {left} more to unlock Build",
                "partial": f"last week only partly done - doesn't count ({s.progress}/{BASE_WEEKS_REQUIRED} base weeks)",
                "missed": f"last week missed - one base week lost ({s.progress}/{BASE_WEEKS_REQUIRED})",
                "recovery": f"after recovery, back to base ({s.progress}/{BASE_WEEKS_REQUIRED} completed)"}[verdict]
    return {"completed": f"build week completed ({s.progress} this phase)",
            "partial": "last build week only partly done - repeat the level",
            "missed": f"last week missed ({s.missed_streak} in a row; {MISSED_STREAK_RESET} resets to Base)",
            "recovery": "after recovery, next build block"}[verdict]


# --- data ---

def _history(conn, start: date, end: date) -> tuple[dict, dict, dict]:
    """Per week: (load, training days); per day: CTL and TSB."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT start_time::date AS day, SUM(COALESCE(training_load, 0)) AS load
            FROM activities WHERE superseded_by IS NULL AND start_time::date BETWEEN %s AND %s
            GROUP BY 1;
        """, (start, end))
        days = {r["day"]: float(r["load"]) for r in cur.fetchall()}
        cur.execute("""
            SELECT day, ctl, atl FROM daily_metrics
            WHERE ctl IS NOT NULL AND day BETWEEN %s AND %s ORDER BY day;
        """, (start - timedelta(days=14), end))
        rows = cur.fetchall()
    weeks: dict[date, list] = {}
    for d, load in days.items():
        w = weeks.setdefault(monday(d), [0.0, 0])
        w[0] += load
        w[1] += 1 if load > 0 else 0
    ctl = {r["day"]: float(r["ctl"]) for r in rows}
    tsb = {r["day"]: float(r["ctl"]) - float(r["atl"]) for r in rows if r["atl"] is not None}
    return weeks, ctl, tsb


def _at(series: dict, d: date, default=None):
    """Value on day d, or the closest earlier day (up to 14 days back)."""
    for i in range(15):
        v = series.get(d - timedelta(days=i))
        if v is not None:
            return v
    return default


def compute_plan(conn, today: date | None = None, weeks_ahead: int = 12) -> list[dict]:
    today = today or date.today()
    this_monday = monday(today)
    weeks, ctl, tsb = _history(conn, REPLAY_FROM, today)

    s, rows = State(), []
    last_ctl = 0.0
    reason = "start of the replay - Base week 1"
    wk = REPLAY_FROM
    while wk < this_monday:
        ctl_start = _at(ctl, wk - timedelta(days=1), last_ctl)
        ctl_end = _at(ctl, wk + timedelta(days=6), ctl_start)
        kind = week_kind(s, _at(tsb, wk - timedelta(days=1)))
        lo, hi = week_targets(kind, ctl_start)
        load, days = weeks.get(wk, (0.0, 0))
        verdict = judge(kind, load, days, lo, hi)
        rows.append(_row(wk, s.phase, kind, lo, hi, verdict, reason, load))
        reason = advance(s, kind, verdict, load, hi, ctl_end)
        last_ctl = ctl_end
        wk += timedelta(weeks=1)

    # This week and the projection: assume each week gets completed at the middle of its range.
    proj_ctl, proj_tsb = _at(ctl, this_monday - timedelta(days=1), last_ctl), _at(tsb, this_monday - timedelta(days=1))
    for i in range(weeks_ahead + 1):
        kind = week_kind(s, proj_tsb if i == 0 else None)
        lo, hi = week_targets(kind, proj_ctl)
        rows.append(_row(wk, s.phase, kind, lo, hi, None,
                         reason if i == 0 else "projected - assumes the weeks before are completed", None))
        mid = (lo + hi) / 2
        verdict = judge(kind, mid, 7, lo, hi)
        proj_ctl += (mid / 7 - proj_ctl) * CTL_GAIN_PER_WEEK
        reason = advance(s, kind, verdict, mid, hi, proj_ctl)
        wk += timedelta(weeks=1)
    return rows


def _row(wk, phase, kind, lo, hi, verdict, reason, load) -> dict:
    deload = kind == "recovery"
    return {
        "start_date": wk, "end_date": wk + timedelta(days=6),
        "phase": "Recovery" if deload else phase, "is_deload": deload,
        "target_weekly_load_min": lo, "target_weekly_load_max": hi,
        "primary_focus": FOCUS[kind], "sessions": json.dumps(SESSIONS[kind]),
        "status": verdict, "notes": reason, "actual_load": round(load) if load is not None else None,
    }


def update_plan(conn, today: date | None = None) -> dict:
    """Recomputes and stores the plan. Returns {'before': old this-week row, 'now': new one}."""
    today = today or date.today()
    rows = compute_plan(conn, today)
    this_monday = monday(today)
    with conn.cursor() as cur:
        cur.execute("SELECT phase, is_deload, notes FROM training_blocks WHERE start_date = %s;", (this_monday,))
        before = cur.fetchone()
        cur.execute("DELETE FROM training_blocks WHERE start_date >= %s;", (REPLAY_FROM,))
        for r in rows:
            cur.execute("""
                INSERT INTO training_blocks (phase, start_date, end_date, target_weekly_load_min,
                    target_weekly_load_max, primary_focus, is_deload, sessions, status, notes)
                VALUES (%(phase)s, %(start_date)s, %(end_date)s, %(target_weekly_load_min)s,
                    %(target_weekly_load_max)s, %(primary_focus)s, %(is_deload)s, %(sessions)s, %(status)s, %(notes)s);
            """, r)
    conn.commit()
    now = next(r for r in rows if r["start_date"] == this_monday)
    return {"before": dict(before) if before else None, "now": now, "rows": rows}
