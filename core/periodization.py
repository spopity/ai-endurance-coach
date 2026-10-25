"""
Adaptive periodization: the phase of each week is earned from what was actually
trained, not read off a calendar fixed in advance.

The engine replays every closed week since REPLAY_FROM, judges it against the
target it had (completed / partial / missed / recovery) and moves a small state
machine:

- Base comes first. Build unlocks once BASE_WEEKS_REQUIRED base weeks were
  *completed* within the last BASE_WINDOW weeks. Partial and missed weeks simply
  don't count - they don't subtract, because a week of life happening costs very
  little fitness (two weeks off lower VO2max measurably but modestly).
- The only step back is a real loss of fitness: CTL falling CTL_DROP_RESET below
  its peak in the current phase. It costs about one base week per 10% of CTL lost
  (from Build: back to Base with that many base weeks to redo), not a reset to week 1.
- A recovery week comes when form or HRV say so after at least MIN_LOAD_WEEKS
  loading weeks (TSB below RECOVERY_TSB, HRV 7-day average well below baseline, or
  HRV below baseline with tired form), and at the latest after MAX_LOAD_WEEKS. Only a
  week at or above the load that holds fitness (~7 x CTL) counts as loading - a light
  week isn't followed by a deload. A missed week already acts as rest.
- Weekly load targets come from the current CTL (the load that holds fitness is
  ~7 x CTL) plus a phase-specific CTL ramp, so they rescale automatically after a
  break instead of asking for the load of a fitter past self.
- Base keeps a small dose of intensity (sprints / short VO2 touches once a week,
  tempo in late Base); full VO2max interval sessions are Build-only.
- Races in the Intervals.icu calendar anchor the plan, planned backwards like a pro
  season: an A race gets race-specific preparation 2-RACE_PREP_WEEKS weeks out, a taper
  the week before, a race week with openers and a recovery week after; within
  RACE_BUILD_WEEKS of it, Build starts early (from BASE_EARLY_WEEKS completed base
  weeks). A B race gets a mini-taper in its own week; C races are just hard sessions.
  Without races the plan works towards fitness and FTP (GOAL_NO_RACE).
- Days the athlete can't train (core/availability.py: sick / away) shrink that week's
  targets pro rata; a week with at most 2 days available is "away" - rest, nothing lost.

return_status() turns the training days into a graded return-to-training stage
(short break / break / long break / easing back in / after illness) for the coach.

The result is written to training_blocks (past weeks with their verdict, this
week, and a projection of the coming weeks assuming they get completed), so the
dashboard, briefing and coach read it as before. Re-running is idempotent; the
current week only depends on closed weeks, so it doesn't change mid-week.
"""
import json
import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger("AI_COACH_V1.periodization")

REPLAY_FROM = date(2026, 1, 5)       # fixed anchor, so the replay is deterministic
BASE_WEEKS_REQUIRED = 6              # completed base weeks before Build unlocks...
BASE_WINDOW = 10                     # ...within this many of the latest base weeks
BASE_EARLY_WEEKS = 3                 # first base weeks: aerobic + sprints, no tempo yet
MIN_LOAD_WEEKS = 2                   # a readiness-driven recovery week needs at least this many loading weeks
MAX_LOAD_WEEKS = 4                   # ...and comes at the latest after this many
PROJECTED_LOAD_WEEKS = 3             # the projection can't know future form/HRV - assume a deload after 3
RECOVERY_TSB = -25                   # form this tired calls for recovery
TIRED_TSB = -15                      # HRV below baseline + form this tired calls for recovery too
CTL_DROP_RESET = 0.25                # CTL 25% below the phase peak -> step back
CTL_DROP_PER_WEEK = 0.10             # each 10% of CTL lost costs one base week
RACE_PREP_WEEKS = 6                  # A race: race-specific weeks 2..6 before it
RACE_BUILD_WEEKS = 12                # A race within this: Build may start before Base is done
RACE_DEFER_RECOVERY_WEEKS = 3        # this close to an A race the taper replaces a calendar deload
AWAY_MIN_DAYS = 5                    # this many days unavailable makes the week "away"
GOAL_NO_RACE = ("Fitness and FTP - no A/B race on the calendar, so Build works on FTP "
                "(sweet spot, threshold, VO2max).")
# Weekly load as a share of the load that holds fitness (7 x CTL).
RACE_TARGETS = {"race_a": (0.55, 0.8), "taper": (0.7, 0.85), "race_b": (0.8, 1.0)}
RACE_PHASES = {"race_a": "Race", "race_b": "Race", "taper": "Taper", "specific": "Race prep"}

# Planned CTL rise per week; weekly TSS to get it is ~ 7*CTL + 45*ramp
# (Banister, 42-day CTL: one week of constant daily load L moves CTL by ~0.154*(L - CTL)).
RAMP = {"Base": 2.0, "Build": 3.0}
CTL_GAIN_PER_WEEK = 1 - math.exp(-7 / 42)
MIN_TARGET = 120                     # loading-week floor (TSS) so a near-zero CTL still gets a real week

# Verdicts for loading weeks, relative to the week's target minimum.
COMPLETE_SHARE, MISSED_SHARE = 0.85, 0.5
MIN_TRAINING_DAYS = 3

# Return to training after days without training (graded, see return_status()).
SHORT_BREAK_DAYS, BREAK_DAYS, LONG_BREAK_DAYS = 3, 7, 14
EASY_SESSIONS_AFTER = {BREAK_DAYS: 2, LONG_BREAK_DAYS: 3}  # easy sessions before the plan resumes
NOT_TRAINING = ("Walk",)

FOCUS = {
    "base_early": "Base - aerobic foundation: mostly Zone 2 endurance and cadence work. Once a week keep the top end "
                  "alive inside an endurance ride: 6-8 x 15 s sprints or 4-5 x 1 min hard (under 10 min hard in "
                  "total). No interval sessions yet. Consistency matters more than any single session.",
    "base_late": "Base - aerobic + tempo: mostly Zone 2, one tempo / low sweet spot session, and once a week short VO2 "
                 "touches inside an endurance ride (e.g. 5 x 2 min hard, under 10 min in total). No full VO2max "
                 "interval sessions yet.",
    "build_1": "Build - threshold development: sweet spot and FTP-building intervals.",
    "build_2": "Build - threshold progression: longer sweet spot / threshold intervals.",
    "build_3": "Build - peak load week: VO2max/anaerobic work at the top of this block before recovery.",
    "recovery": "Deload - reduced volume and intensity to absorb the last weeks. No high-intensity intervals this week.",
    "specific": "Race prep - race-specific work: efforts that match the race's demands (threshold, VO2max and "
                "race-pace blocks) and one long ride with race-pace sections. Quality over volume.",
    "taper": "Taper - volume down about a third, intensity kept: short race-pace and VO2 efforts (e.g. 3-4 x 2 min) "
             "stay in so you arrive fresh and sharp. No long or hard-fatiguing sessions.",
    "race_a": "Race week (A race) - short easy rides with openers (e.g. 3 x 1 min at race pace + 2 short sprints) on "
              "the days before, an easy day or rest the day before. The race is the week's key session.",
    "race_b": "Race week (B race) - normal training until 3 days out, then easy with openers. The race is the "
              "week's hard session.",
}
# Planned session mix (cycling focus - dual mode adds runs on top, see core/focus.py).
SESSIONS = {
    "base_early": {"endurance": 4, "sst": 0, "vo2": 0, "run": 1, "weights": 3},
    "base_late": {"endurance": 3, "sst": 1, "vo2": 0, "run": 1, "weights": 4},
    "build_1": {"endurance": 3, "sst": 1, "vo2": 0, "run": 1, "weights": 4},
    "build_2": {"endurance": 2, "sst": 2, "vo2": 0, "run": 1, "weights": 4},
    "build_3": {"endurance": 2, "sst": 1, "vo2": 1, "run": 1, "weights": 3},
    "recovery": {"endurance": 3, "sst": 0, "vo2": 0, "run": 1, "weights": 3},
    "specific": {"endurance": 2, "sst": 1, "vo2": 1, "run": 1, "weights": 2},
    "taper": {"endurance": 2, "sst": 0, "vo2": 1, "run": 1, "weights": 1},
    "race_a": {"endurance": 2, "sst": 0, "vo2": 0, "run": 0, "weights": 0},
    "race_b": {"endurance": 2, "sst": 1, "vo2": 0, "run": 1, "weights": 2},
}


@dataclass
class State:
    phase: str = "Base"          # 'Base' | 'Build'
    base_weeks: list = field(default_factory=list)  # verdicts of the base weeks so far, oldest first
    credit: int = 0              # base weeks kept after a step back from Build / a CTL drop
    build_step: int = 0          # completed build weeks since the last rest (build_1/2/3)
    load_weeks: int = 0          # loading weeks since the last rest
    ctl_peak: float = 0.0        # highest CTL seen in this phase

    @property
    def progress(self) -> int:
        """Completed base weeks that count towards Build."""
        recent = self.base_weeks[-BASE_WINDOW:].count("completed")
        return min(BASE_WEEKS_REQUIRED, self.credit + recent)


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def hrv_trend(series: dict, d: date) -> dict | None:
    """HRV 7-day average vs the 60 days before it, as of day d: status 'low' (< baseline - 1 SD),
    'below' (< baseline - 0.5 SD) or 'normal'. Same verdict as the readiness check."""
    recent = [v for day, v in series.items() if d - timedelta(days=7) < day <= d]
    base = [v for day, v in series.items() if d - timedelta(days=67) < day <= d - timedelta(days=7)]
    if len(recent) < 2 or len(base) < 10:
        return None
    mean, sd = statistics.fmean(base), statistics.stdev(base)
    avg7 = statistics.fmean(recent)
    status = "low" if avg7 < mean - sd else "below" if avg7 < mean - 0.5 * sd else "normal"
    return {"avg7": avg7, "baseline": mean, "sd": sd, "status": status}


AFTER_ILLNESS_EASY_SESSIONS = 2


def return_status(training_days: list[date], today: date, sick_until: date | None = None,
                  illness_end: date | None = None) -> dict:
    """Graded return to training from the days with training (sorted). Always has days_off
    and stage; guidance tells the coach what the next session(s) should be. `sick_until`:
    the athlete is ill today (last saved sick day); `illness_end`: the last sick day of a
    recent illness (core/availability.py)."""
    past = [d for d in training_days if d <= today]
    last = past[-1] if past else None
    days_off = max(0, (today - last).days - 1) if last else None  # full days without training before today
    out = {"days_off": days_off, "last_training_day": last.isoformat() if last else None}
    if sick_until:
        return {**out, "stage": "ill", "guidance": (
            f"Ill - no training while sick (saved until {sick_until.isoformat()}). The next session is rest; plan "
            f"the first session for {(sick_until + timedelta(days=1)).isoformat()} or later, easy.")}
    if illness_end:
        done = sum(1 for d in past if d > illness_end)
        if done < AFTER_ILLNESS_EASY_SESSIONS:
            left = AFTER_ILLNESS_EASY_SESSIONS - done
            return {**out, "stage": "after_illness", "guidance": (
                f"Ill until {illness_end.isoformat()}. {left} more easy Zone 1-2 session{'s' if left != 1 else ''} "
                "(short, 45-60 min) before any intensity - and only once you feel fully normal; if resting HR or "
                "HRV are still off, stay easy longer.")}
    if not past:
        return {**out, "stage": "on_plan", "guidance": "No training history yet."}
    if days_off >= LONG_BREAK_DAYS:
        return {**out, "stage": "long_break", "guidance": (
            f"{days_off} days off. Next session easy Zone 1-2 (about 60-75% of a normal duration), and the first "
            f"{EASY_SESSIONS_AFTER[LONG_BREAK_DAYS]} sessions back stay easy. Then follow the plan, which has already "
            "stepped back if fitness dropped (see current_training_block.why).")}
    if days_off >= BREAK_DAYS:
        return {**out, "stage": "break", "guidance": (
            f"{days_off} days off. Next session easy Zone 1-2 (about 60-75% of a normal duration); the first "
            f"{EASY_SESSIONS_AFTER[BREAK_DAYS]} sessions back stay easy. Then the plan resumes at about 75% of the "
            "weekly target for the rest of that week.")}
    if days_off >= SHORT_BREAK_DAYS:
        return {**out, "stage": "short_break", "guidance": (
            f"{days_off} days off - that costs almost no fitness. Next session: a normal endurance session at normal "
            "duration, no intervals in this one. From the session after it, straight back to the plan.")}

    # Training again - still easing back in after a longer break?
    for prev, nxt in zip(reversed(past[:-1]), reversed(past)):
        gap = (nxt - prev).days - 1
        if (today - nxt).days > LONG_BREAK_DAYS:
            break
        if gap >= BREAK_DAYS:
            needed = EASY_SESSIONS_AFTER[LONG_BREAK_DAYS if gap >= LONG_BREAK_DAYS else BREAK_DAYS]
            done = sum(1 for d in past if d >= nxt)
            if done < needed:
                left = needed - done
                return {**out, "stage": "easing_back_in", "guidance": (
                    f"Back after {gap} days off, {done} session{'s' if done != 1 else ''} done since. "
                    f"{left} more easy Zone 1-2 session{'s' if left != 1 else ''}, then the plan"
                    + (" at about 75% of the weekly target for the rest of this week." if gap < LONG_BREAK_DAYS else "."))}
            if gap < LONG_BREAK_DAYS and monday(nxt) == monday(today):
                return {**out, "stage": "on_plan", "guidance": (
                    f"Back after {gap} days off and eased in - follow the plan at about 75% of the weekly target "
                    "for the rest of this week, then fully.")}
            break
    return {**out, "stage": "on_plan", "guidance": "Training normally - follow the plan."}


def week_targets(kind: str, ctl: float, available_days: int = 7) -> tuple[float, float]:
    """Weekly TSS range, scaled down pro rata for days the athlete can't train."""
    hold = 7 * ctl
    if kind == "recovery":
        lo, hi = max(hold * 0.5, 60), max(hold * 0.7, 90)
    elif kind in RACE_TARGETS:
        share_lo, share_hi = RACE_TARGETS[kind]
        lo, hi = max(hold * share_lo, 60), max(hold * share_hi, 90)
    else:
        phase = "Base" if kind.startswith("base") else "Build"
        center = max(hold + 7 * RAMP[phase] / CTL_GAIN_PER_WEEK, MIN_TARGET)
        lo, hi = center * 0.9, center * 1.1
    scale = available_days / 7
    return round(lo * scale), round(hi * scale)


def race_plan(wk: date, races: list[dict]) -> tuple[str | None, str | None, int | None]:
    """(race-driven kind or None, why, weeks to the next A race or None) for the week starting wk.
    Only A and B races shape the plan; C races are treated as a hard session."""
    def week_of(r):
        return monday(date.fromisoformat(r["date"]))

    key = [r for r in races if r.get("priority") in ("A", "B") and r.get("date")]
    here = sorted((r for r in key if week_of(r) == wk), key=lambda r: r["priority"])
    next_a = next((r for r in sorted(key, key=lambda r: r["date"]) if r["priority"] == "A" and week_of(r) > wk), None)
    weeks_out = (week_of(next_a) - wk).days // 7 if next_a else None
    if here:
        r = here[0]
        return f"race_{r['priority'].lower()}", f"{r['priority']} race {r['name']} on {r['date']} - race week", 0
    if weeks_out == 1:
        return "taper", f"A race {next_a['name']} on {next_a['date']} next week - taper", 1
    prev_a = next((r for r in key if r["priority"] == "A" and week_of(r) == wk - timedelta(weeks=1)), None)
    if prev_a:
        return "recovery", f"after the A race {prev_a['name']} - recovery week", weeks_out
    if weeks_out is not None and weeks_out <= RACE_PREP_WEEKS:
        return "specific", f"A race {next_a['name']} on {next_a['date']} in {weeks_out} weeks - race-specific preparation", weeks_out
    if weeks_out is not None and weeks_out <= RACE_BUILD_WEEKS:
        return None, f"A race {next_a['name']} on {next_a['date']} in {weeks_out} weeks", weeks_out
    return None, None, weeks_out


def recovery_signal(tsb: float | None, hrv: dict | None) -> str | None:
    """Why form/HRV call for a recovery week, or None."""
    if tsb is not None and tsb < RECOVERY_TSB:
        return f"form very tired (TSB {tsb:.0f})"
    if hrv and hrv["status"] == "low":
        return "HRV well below baseline"
    if hrv and hrv["status"] == "below" and tsb is not None and tsb < TIRED_TSB:
        return f"HRV below baseline with tired form (TSB {tsb:.0f})"
    return None


def week_kind(s: State, tsb: float | None, hrv: dict | None, projected: bool = False,
              race: tuple = (None, None, None)) -> tuple[str, str | None]:
    """(kind, why - for a recovery or race-driven week) for the week about to start.
    `race` is race_plan() for that week: race week, taper and post-race recovery win;
    race-specific weeks still give way to a recovery week that form or HRV call for."""
    race_kind, race_why, weeks_out = race
    if race_kind in ("race_a", "race_b", "taper") or race_kind == "recovery":
        return race_kind, race_why
    near_race = weeks_out is not None and weeks_out <= RACE_DEFER_RECOVERY_WEEKS
    if race_kind == "specific":
        if not projected and s.load_weeks >= MIN_LOAD_WEEKS and (signal := recovery_signal(tsb, hrv)):
            return "recovery", f"{signal} after {s.load_weeks} loading weeks - recovery week, then back to race prep"
        if s.load_weeks >= (PROJECTED_LOAD_WEEKS if projected else MAX_LOAD_WEEKS) and not near_race:
            return "recovery", f"{s.load_weeks} loading weeks in a row - recovery week, then back to race prep"
        return "specific", race_why
    if projected:
        if s.load_weeks >= PROJECTED_LOAD_WEEKS:
            return "recovery", f"projected deload after {s.load_weeks} loading weeks (the real timing follows form and HRV)"
    elif s.load_weeks >= MAX_LOAD_WEEKS:
        return "recovery", f"{s.load_weeks} loading weeks in a row - recovery week"
    elif s.load_weeks >= MIN_LOAD_WEEKS and (signal := recovery_signal(tsb, hrv)):
        return "recovery", f"{signal} after {s.load_weeks} loading weeks - recovery week"
    if s.phase == "Base":
        if race_why and s.progress >= BASE_EARLY_WEEKS:  # an A race within RACE_BUILD_WEEKS
            return "build_1", f"{race_why} - Build starts early ({s.progress}/{BASE_WEEKS_REQUIRED} base weeks)"
        return ("base_early" if s.progress < BASE_EARLY_WEEKS else "base_late"), race_why
    return f"build_{min(s.build_step, 2) + 1}", race_why


def judge(kind: str, load: float, days: int, lo: float, hi: float, available_days: int = 7) -> str:
    if kind in ("recovery", "race_a", "taper"):
        return "recovery" if kind == "recovery" else "race"
    if available_days <= 7 - AWAY_MIN_DAYS:
        return "away"
    if load >= COMPLETE_SHARE * lo and days >= min(MIN_TRAINING_DAYS, available_days):
        return "completed"
    if load < MISSED_SHARE * lo or days <= 1:
        return "missed"
    return "partial"


def advance(s: State, kind: str, verdict: str, load: float, hi: float, hold: float, ctl_end: float) -> str:
    """Moves the state past a closed week; returns why the next week is what it is.
    `hold` is the load that keeps fitness (7 x CTL at the week's start)."""
    s.ctl_peak = max(s.ctl_peak, ctl_end)
    if s.phase == "Base" and verdict not in ("away", "race"):  # weeks away don't use up the window
        s.base_weeks.append(verdict)
    if kind == "specific" or (kind == "build_1" and s.phase == "Base"):
        # Race prep, or Build started early for an A race: from here on the athlete is in Build.
        s.phase, s.base_weeks, s.credit = "Build", [], 0
    if verdict in ("recovery", "race"):
        # A "recovery" week trained well above its cap wasn't rest.
        s.load_weeks = 1 if load > hi * 1.3 else 0
        s.build_step = 0
    elif verdict in ("missed", "away"):  # counts as rest
        s.load_weeks, s.build_step = 0, 0
    else:
        if load >= hold:  # only a week that at least held fitness was a loading week
            s.load_weeks += 1
        if verdict == "completed" and s.phase == "Build":
            s.build_step += 1

    if s.ctl_peak and ctl_end < s.ctl_peak * (1 - CTL_DROP_RESET):
        peak, drop = s.ctl_peak, 1 - ctl_end / s.ctl_peak
        weeks_lost = math.ceil(round(drop / CTL_DROP_PER_WEEK, 6))
        before = BASE_WEEKS_REQUIRED if s.phase == "Build" else s.progress
        s.credit = max(0, before - weeks_lost)
        s.phase, s.base_weeks, s.ctl_peak, s.build_step = "Base", [], ctl_end, 0
        return (f"fitness dropped {drop:.0%} (CTL {peak:.0f} -> {ctl_end:.0f}) - {weeks_lost} base weeks to redo, "
                f"{s.progress}/{BASE_WEEKS_REQUIRED} base weeks")
    if s.phase == "Base" and s.progress >= BASE_WEEKS_REQUIRED:
        s.phase, s.base_weeks, s.credit, s.ctl_peak, s.build_step = "Build", [], 0, ctl_end, 0
        return f"{BASE_WEEKS_REQUIRED} base weeks completed - Build unlocked"

    if s.phase == "Base":
        left = BASE_WEEKS_REQUIRED - s.progress
        status = f"{s.progress}/{BASE_WEEKS_REQUIRED} base weeks, {left} more to unlock Build"
        return {"completed": f"base week completed ({status})",
                "partial": f"last week only partly done - doesn't count, nothing lost ({status})",
                "missed": f"last week missed - counts as rest, nothing lost ({status})",
                "away": f"last week away / ill - counts as rest, nothing lost ({status})",
                "recovery": f"after recovery, back to base ({status})",
                "race": f"after the race week, back to base ({status})"}[verdict]
    return {"completed": f"build week completed ({s.build_step} since the last rest)",
            "partial": "last build week only partly done - repeat the level",
            "missed": "last week missed - counts as rest, next build block starts at level 1",
            "away": "last week away / ill - counts as rest, next build block starts at level 1",
            "recovery": "after recovery, next build block",
            "race": "after the race / taper week"}[verdict]


# --- data ---

def _history(conn, start: date, end: date) -> tuple[dict, dict, dict, dict]:
    """Per week: (load, training days); per day: CTL, TSB and HRV."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT start_time::date AS day, SUM(COALESCE(training_load, 0)) AS load
            FROM activities WHERE superseded_by IS NULL AND start_time::date BETWEEN %s AND %s
            GROUP BY 1;
        """, (start, end))
        days = {r["day"]: float(r["load"]) for r in cur.fetchall()}
        cur.execute("""
            SELECT day, ctl, atl, hrv FROM daily_metrics
            WHERE day BETWEEN %s AND %s ORDER BY day;
        """, (start - timedelta(days=70), end))
        rows = cur.fetchall()
    weeks: dict[date, list] = {}
    for d, load in days.items():
        w = weeks.setdefault(monday(d), [0.0, 0])
        w[0] += load
        w[1] += 1 if load > 0 else 0
    ctl = {r["day"]: float(r["ctl"]) for r in rows if r["ctl"] is not None}
    tsb = {r["day"]: float(r["ctl"]) - float(r["atl"]) for r in rows if r["ctl"] is not None and r["atl"] is not None}
    hrv = {r["day"]: float(r["hrv"]) for r in rows if r["hrv"] is not None}
    return weeks, ctl, tsb, hrv


def training_days(conn, since: date) -> list[date]:
    """Days with training load (walks don't count), oldest first - for return_status()."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT start_time::date AS day FROM activities
            WHERE superseded_by IS NULL AND training_load > 0 AND type <> ALL(%s) AND start_time::date >= %s
            ORDER BY 1;
        """, (list(NOT_TRAINING), since))
        return [r["day"] for r in cur.fetchall()]


def _at(series: dict, d: date, default=None):
    """Value on day d, or the closest earlier day (up to 14 days back)."""
    for i in range(15):
        v = series.get(d - timedelta(days=i))
        if v is not None:
            return v
    return default


def load_races(today: date) -> list[dict]:
    """A/B/C races from the replay start to a year ahead (Intervals.icu); [] if unreachable."""
    from core.races import fetch_races  # lazy: keeps this module importable without the API
    try:
        return fetch_races(REPLAY_FROM, today + timedelta(days=365))
    except Exception as e:
        logger.warning(f"Races unavailable - planning without them: {e}")
        return []


def compute_plan(conn, today: date | None = None, weeks_ahead: int = 12, races: list[dict] | None = None,
                 availability: dict | None = None) -> list[dict]:
    """The replayed and projected plan. `races` (default: from Intervals.icu) anchor it;
    `availability` (default: core/availability.py) shrinks weeks with days off."""
    from core import availability as avail
    today = today or date.today()
    this_monday = monday(today)
    weeks, ctl, tsb, hrv = _history(conn, REPLAY_FROM, today)
    races = load_races(today) if races is None else races
    availability = avail.load(today) if availability is None else availability

    def available(wk: date, after: date | None = None) -> int:
        """Days in the week (after `after`, if given) the athlete can train."""
        first = max(wk, after + timedelta(days=1)) if after else wk
        last = wk + timedelta(days=6)
        if first > last:
            return 0
        return (last - first).days + 1 - len(avail.unavailable_days(first, last, availability))

    s, rows = State(), []
    last_ctl = 0.0
    reason = "start of the replay - Base week 1"
    wk = REPLAY_FROM
    while wk < this_monday:
        eve = wk - timedelta(days=1)
        ctl_start = _at(ctl, eve, last_ctl)
        ctl_end = _at(ctl, wk + timedelta(days=6), ctl_start)
        kind, why = week_kind(s, _at(tsb, eve), hrv_trend(hrv, eve), race=race_plan(wk, races))
        free = available(wk)
        lo, hi = week_targets(kind, ctl_start, free)
        load, days = weeks.get(wk, (0.0, 0))
        verdict = judge(kind, load, days, lo, hi, free)
        rows.append(_row(wk, s.phase, kind, lo, hi, verdict, _why(why, reason, free), load))
        reason = advance(s, kind, verdict, load, hi, 7 * ctl_start, ctl_end)
        last_ctl = ctl_end
        wk += timedelta(weeks=1)

    # This week (decided from last Sunday's form and HRV) and the projection: future weeks are
    # assumed completed at the middle of their range; this week is what's done so far plus the
    # middle of the range for the days still to come (so a quiet week isn't projected as completed).
    eve = this_monday - timedelta(days=1)
    proj_ctl = _at(ctl, eve, last_ctl)
    for i in range(weeks_ahead + 1):
        race = race_plan(wk, races)
        if i == 0:
            kind, why = week_kind(s, _at(tsb, eve), hrv_trend(hrv, eve), race=race)
        else:
            kind, why = week_kind(s, None, None, projected=True, race=race)
        free = available(wk)
        lo, hi = week_targets(kind, proj_ctl, free)
        rows.append(_row(wk, s.phase, kind, lo, hi, None, _why(why, reason, free) if i == 0 else _why(
            None, why or "projected - assumes the weeks before are completed", free), None))
        load, days = (lo + hi) / 2, free
        if i == 0:
            days_left = available(wk, after=today)
            done, days_done = weeks.get(wk, (0.0, 0))
            load, days = done + (lo + hi) / 2 * days_left / max(free, 1), days_done + days_left
        verdict = judge(kind, load, days, lo, hi, free)
        hold = 7 * proj_ctl
        proj_ctl += (load / 7 - proj_ctl) * CTL_GAIN_PER_WEEK
        reason = advance(s, kind, verdict, load, hi, hold, proj_ctl)
        wk += timedelta(weeks=1)
    return rows


def _why(why: str | None, reason: str, available_days: int = 7) -> str:
    text = f"{why}; {reason}" if why else reason
    if available_days < 7:
        text += f"; {7 - available_days} day{'s' if 7 - available_days != 1 else ''} unavailable - target scaled down"
    return text


def _row(wk, phase, kind, lo, hi, verdict, reason, load) -> dict:
    deload = kind == "recovery"
    return {
        "start_date": wk, "end_date": wk + timedelta(days=6),
        "phase": RACE_PHASES.get(kind) or ("Recovery" if deload else phase), "is_deload": deload,
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
