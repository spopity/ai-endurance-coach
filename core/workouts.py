"""
Structured workouts from the rolling 7-day plan (core/schedule.py), pushed to the
Intervals.icu calendar only when the athlete says so.

Every morning the Telegram bot builds the workouts for the plan's first day (after the
briefing) and asks Push / Skip for each; the dashboard shows the same workouts with the same
buttons. Pushing creates a planned WORKOUT event on Intervals.icu from the workout text below
(Intervals parses it into steps, so it syncs on to Garmin/Zwift like any planned workout and
can be downloaded as .zwo/.fit/.mrc from there).

Workout text (Intervals.icu workout builder syntax):
- rides use % of FTP (Intervals' own FTP setting), runs use Intervals' run HR zones ("Z2 HR");
  Zone 2 rides go up to just under the measured LT1 (core/thresholds.py), sub-threshold runs use
  % of LTHR ("94-97% LTHR"), the FTP test's warm-up is a 4-step LT1 ramp, the CS test is
  3 + 9 min all-out,
- gym sessions become WeightTraining events with the prescription as text, no steps,
- a pinned session (the athlete's own words) is converted to steps by Gemini (temperature 0,
  told to keep exactly what was written: reps, durations, intensities; watts -> % FTP, bpm ->
  the HR zone that contains it, as Intervals doesn't read absolute bpm). The result is checked
  in code before it is used: every step needs a duration and a target, every "N x M" in the
  athlete's text must come back as an N-rep block with an M-long step, and the total must fit
  a total the text states. Anything failing falls back to the plain text. Conversions are
  cached in state/workouts/pinned.json by text + FTP + HR zones; failures retry after an hour,
- every workout starts with a fueling line (core/nutrition.py): carbs before / per hour during
  / protein and carbs after, from duration, intensity and weight,
- amber readiness drops one rep of the main set and adds the warm-up test; a "max N min" day
  shortens warm-up/cool-down first, then drops reps; red readiness is already short and easy
  in the plan.

Planned vs done: Intervals.icu pairs each activity with the planned workout of its day and
reports a compliance %. `match()` finds the pushed workout behind an activity; the 7-day plan
counts a session at >= 80% compliance as done (core/schedule._done), and the Strava note shows
the planned workout and the compliance.

The state lives in state/workouts/<date>.json: per session the last pushed text, the event id
on Intervals and the status (draft / pushed / skipped). The workout is rebuilt from the plan on
every read; when the plan's prescription changed since the push, `changed` is set and Push
updates the same event (otherwise the pushed text is shown as it is on Intervals). A pushed
session the plan no longer has (done already, moved) is kept with
`in_plan: false` so it can still be removed.
"""
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta

import requests

from core.db import load_env
from core.nutrition import fuel
from core.schedule import TZ, _week_kind, build as build_schedule

logger = logging.getLogger("AI_COACH_V1.workouts")

BASE_URL = "https://intervals.icu"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state", "workouts")
KEEP_DAYS = 30
EVENT_TYPE = {"ride": "Ride", "run": "Run", "gym": "WeightTraining"}
GYM_MINUTES = {"gym_legs": 45, "gym_upper": 35, "gym_core": 25}
WARMUP_TEST = ("Warm-up test: if HR runs high or the legs feel flat after the warm-up, "
               "ride/run the rest easy instead.")


class WorkoutError(Exception):
    pass


# --- steps ---
# A workout is a list of blocks: (header, reps, [(seconds, target, cue), ...]).

def _dur(sec: int) -> str:
    m, s = divmod(int(sec), 60)
    if not s:
        return f"{m}m"
    return f"{m}m{s}s" if m else f"{s}s"


def _render(blocks: list) -> str:
    out = []
    for header, reps, steps in blocks:
        out.append(f"{header} {reps}x" if reps > 1 else header)
        for sec, target, cue in steps:
            out.append(f"- {cue + ' ' if cue else ''}{_dur(sec)} {target}".rstrip())
        out.append("")
    return "\n".join(out).strip()


def _total(blocks: list) -> int:
    return sum(reps * sum(s[0] for s in steps) for _, reps, steps in blocks)


def _interval_session(wu: int, main: list, reps: int, cd: int, wu_target: str, cd_target: str,
                      extra: list | None = None, min_reps: int = 2, amber: bool = False,
                      max_min: int | None = None) -> list:
    """Warm-up, `reps` x main, optional extra block, cool-down; trimmed for amber / max minutes."""
    if amber:
        reps = max(min_reps, reps - 1)

    def blocks(wu_s, reps_n, cd_s):
        b = [("Warmup", 1, [(wu_s, wu_target, "")]), ("Main set", reps_n, main)]
        if extra:
            b.append(extra)
        b.append(("Cooldown", 1, [(cd_s, cd_target, "")]))
        return b

    b = blocks(wu, reps, cd)
    if max_min:
        limit = max_min * 60
        wu_s, cd_s = wu, cd
        while _total(b) > limit and (wu_s > 600 or cd_s > 300):
            wu_s, cd_s = max(600, wu_s - 300), max(300, cd_s - 300)
            b = blocks(wu_s, reps, cd_s)
        if extra and _total(b) > limit:
            extra = None
            b = blocks(wu_s, reps, cd_s)
        while _total(b) > limit and reps > 1:
            reps -= 1
            b = blocks(wu_s, reps, cd_s)
    return b


# The quality sessions' main set comes from the plan's text (core/schedule.py, sized by the
# progression levels in core/progression.py): "Sweet spot 3x15 min at ..., 5 min easy between".
QUALITY_RE = re.compile(r"^(VO2max|Tempo|Sweet spot|Threshold|Hills)\s+(\d+)x(\d+)\s*(min|s)\b")
RECOVERY_RE = re.compile(r"(\d+) min (?:easy|jog) between")
SUB_T_TARGET = "94-97% LTHR"   # core/schedule.SUB_T_LTHR
BIKE_TARGETS = {"VO2max": "106-120% 95rpm", "Tempo": "76-87%", "Sweet spot": "88-94%", "Threshold": "95-105%"}


def _quality(detail: str) -> tuple[str, int, int, int] | None:
    """(title, reps, seconds on, seconds easy between) from a quality session's plan text."""
    m = QUALITY_RE.match(detail or "")
    if not m:
        return None
    on = int(m.group(3)) * (60 if m.group(4) == "min" else 1)
    rec = RECOVERY_RE.search(detail)
    off = int(rec.group(1)) * 60 if rec else on
    return m.group(1), int(m.group(2)), on, off


def _reps_unit(on: int) -> str:
    return f"{on // 60} min" if on % 60 == 0 and on >= 120 else f"{on} s"


def _steady(minutes: int, target: str, wu_target: str, max_min: int | None, cue: str = "") -> list:
    minutes = min(minutes, max_min) if max_min else minutes
    if minutes < 30:
        return [("Main set", 1, [(minutes * 60, target, cue)])]
    wu, cd = (10, 5) if minutes < 90 else (15, 10)
    return [("Warmup", 1, [(wu * 60, wu_target, "")]),
            ("Main set", 1, [((minutes - wu - cd) * 60, target, cue)]),
            ("Cooldown", 1, [(cd * 60, wu_target, "")])]


def _z2_hi() -> int:
    """Top of the Zone 2 steps in % FTP: 3 points under the plan's Zone 2 top (core/thresholds.z2_top,
    just under LT1), so the watch's band sits inside the zone; 72 without a measured LT1."""
    from core.overview import cached

    def compute():
        from core.schedule import _targets
        return round((_targets().get("z2_top") or 0.75) * 100) - 3
    try:
        return cached("workout_z2_hi", 300, compute)
    except Exception as e:
        logger.warning(f"Zone 2 top unavailable, using 72%: {e}")
        return 72


def _ride_blocks(s: dict, wk: str, deload: bool, amber: bool, max_min: int | None) -> tuple[str, list]:
    k, detail = s["kind"], s["detail"]
    z2 = _z2_hi()
    if k == "ftp_test":
        return ("FTP test 20 min", [
            ("Aerobic steps", 1, [(240, "55% 90rpm", "Steady, seated - LT1 steps"), (240, "65% 90rpm", "Steady"),
                                  (240, "72% 90rpm", "Steady"), (240, "80% 90rpm", "Steady")]),
            ("Openers", 3, [(60, "100% 110rpm", "Fast pedalling"), (60, "50%", "Easy")]),
            ("Blow-out", 1, [(300, "105-110%", "Hard"), (600, "50%", "Easy")]),
            ("Test", 1, [(1200, "95-110%", "All-out 20 min, even pacing - start at FTP and build")]),
            ("Cooldown", 1, [(600, "50%", "")])])
    if k == "bike_q":
        q = _quality(detail)
        if q:
            title, reps, on, off = q
            args = dict(wu_target="Ramp 50-75% 90rpm", cd_target="50%", amber=amber, max_min=max_min)
            target = BIKE_TARGETS[title]
            name = f"{title} {reps}x{_reps_unit(on)}"
            if title == "VO2max":
                return name, _interval_session(900, [(on, target, ""), (on, "50%", "Easy")], reps, 600, min_reps=3, **args)
            if title == "Tempo":
                return name, _interval_session(900, [(on, target, ""), (off, "55%", "Easy")], reps, 600,
                                               extra=("Endurance", 1, [(600, f"62-{z2}%", "")]), min_reps=1, **args)
            return name, _interval_session(900, [(on, target, ""), (off, "55%", "Easy")], reps, 600, **args)
    if k == "long_ride" and detail.startswith("Durability test"):
        m = re.search(r"about (\d+) min", detail)
        z2_min = int(m.group(1)) if m else 90
        return ("Durability test", [
            ("Warmup", 1, [(600, "Ramp 50-65% 90rpm", "")]),
            ("Endurance", 1, [(max(600, z2_min * 60 - 600), f"60-{z2}% 85-95rpm", "Zone 2 until the kJ in the plan")]),
            ("Easy", 1, [(300, "50-55%", "")]),
            ("Test", 1, [(1200, "95-110%", "All-out 20 min, even pacing")]),
            ("Cooldown", 1, [(600, "50%", "")])])
    if k == "long_ride":
        m = re.match(r"(\d+)(?:-(\d+))? min", detail)
        mins = (int(m.group(1)) + int(m.group(2) or m.group(1))) // 10 * 5 if m else (80 if deload else 135)
        if "last 40 min: 2x10 min sweet spot" in detail:
            mins = min(mins, max_min) if max_min else mins
            return (f"Long ride {mins} min + 2x10 SS", [
                ("Warmup", 1, [(900, "Ramp 50-65% 90rpm", "")]),
                ("Endurance", 1, [(max(600, (mins - 50) * 60), f"60-{z2}% 85-95rpm", "")]),
                ("Late efforts", 2, [(600, "88-94%", "Sweet spot on tired legs"), (300, "55%", "Easy")]),
                ("Cooldown", 1, [(300, "50%", "")])])
        return (f"Long ride {mins} min Z2", _steady(mins, f"60-{z2}% 85-95rpm", "50-60%", max_min))
    # easy ride
    if deload:
        return ("Easy ride 50 min", _steady(50, "50-68%", "50%", max_min))
    if "sprints" in detail:
        mins = min(65, max_min or 65)
        return ("Endurance + sprints", [
            ("Warmup", 1, [(900, "Ramp 50-70% 90rpm", "")]),
            ("Sprints", 7, [(15, "150%", "Sprint"), (165, "55%", "Easy")]),
            ("Endurance", 1, [(max(300, mins * 60 - 900 - 1260 - 600), f"62-{z2}% 85-95rpm", "")]),
            ("Cooldown", 1, [(600, "50%", "")])])
    if "2 min hard" in detail:
        mins = min(70, max_min or 70)
        return ("Endurance + 5x2 min", [
            ("Warmup", 1, [(900, "Ramp 50-70% 90rpm", "")]),
            ("Hard efforts", 5, [(120, "106-115%", ""), (180, "55%", "Easy")]),
            ("Endurance", 1, [(max(300, mins * 60 - 900 - 1500 - 600), f"62-{z2}% 85-95rpm", "")]),
            ("Cooldown", 1, [(600, "50%", "")])])
    return ("Endurance ride 65 min", _steady(65, f"62-{z2}% 85-95rpm", "50-60%", max_min))


def _run_blocks(s: dict, wk: str, deload: bool, amber: bool, max_min: int | None) -> tuple[str, list]:
    k, detail = s["kind"], s["detail"]
    if k == "run_test":
        return ("CS test 3+9 min", [
            ("Warmup", 1, [(900, "Z2 HR", "Easy")]),
            ("Strides", 4, [(20, "Z5 HR", "Stride"), (40, "Z1 HR", "Walk/jog")]),
            ("Test 3 min", 1, [(180, "Z5 HR", "All-out 3 min, even pace")]),
            ("Recovery", 1, [(1200, "Z1 HR", "Easy jog or walk - full recovery")]),
            ("Test 9 min", 1, [(540, "Z5 HR", "All-out 9 min, even pace - same route")]),
            ("Cooldown", 1, [(600, "Z1 HR", "")])])
    if k == "run_q" and not detail.startswith("Easy run"):
        strides = ("Strides", 4, [(20, "Z5 HR", "Stride"), (40, "Z1 HR", "Walk/jog")])
        args = dict(wu_target="Z2 HR", cd_target="Z1 HR", amber=amber, max_min=max_min)
        q = _quality(detail)
        title, reps, on, off = q if q else ("Threshold", 3, 480, 120)
        if title == "Hills":
            b = _interval_session(600, [(on, "Z5 HR", "Hard uphill"), (max(90, on * 3 // 2), "Z1 HR", "Jog down")],
                                  reps, 480, min_reps=max(1, reps - 3), **args)
            name = f"Hill repeats {reps}x{_reps_unit(on)}"
        elif title == "Tempo":
            b = _interval_session(600, [(on, "Z3 HR", "Tempo"), (off, "Z1 HR", "Jog")], reps, 480, min_reps=1, **args)
            name = f"Tempo run {reps}x{_reps_unit(on)}"
        else:
            b = _interval_session(600, [(on, SUB_T_TARGET, "Sub-threshold, controlled"), (off, "Z1 HR", "Jog")],
                                  reps, 480, **args)
            name = f"Threshold run {reps}x{_reps_unit(on)}"
        return name, b[:1] + [strides] + b[1:]
    m = re.match(r"(?:Easy run )?(\d+)", detail)
    if k == "long_run" and not detail.startswith("Easy run"):
        m = re.match(r"(\d+)(?:-(\d+))? min", detail)
        mins = (int(m.group(1)) + int(m.group(2) or m.group(1))) // 2 if m else (45 if deload else 65)
        return f"Long run {mins} min easy", _steady(mins, "Z2 HR", "Z1 HR", max_min, "Conversational")
    mins = int(m.group(1)) if m else 40
    mins = 40 if mins == 35 else mins          # "35-45 min" -> 40
    b = _steady(mins, "Z2 HR", "Z1 HR", max_min)
    if "strides" in detail:
        b.append(("Strides", 6, [(20, "Z5 HR", "Stride"), (60, "Z1 HR", "Walk/jog")]))
    return f"Easy run {mins} min", b


# --- pinned sessions (the athlete's own words) -> steps ---

PIN_CACHE = os.path.join(STATE_DIR, "pinned.json")
PIN_RETRY_SEC = 3600
PIN_PROMPT = """You convert one training session, written by the athlete, into Intervals.icu workout-builder text.
Keep EXACTLY what the athlete wrote: the same reps, durations, recoveries and intensities. Do not add, drop
or change anything. Where the text gives a range ("6-8 min", "42-45 min") use a value inside it. Where it
leaves a duration open, use a sensible one that keeps the stated total.

Sport: {sport}
Athlete's session: {text}
{targets}

Syntax (one step per line, sections separated by a blank line):
Warmup
- 10m {easy}

Main set 3x
- Threshold 7m {hard}
- Jog 2m {rest}

Cooldown
- 8m {rest}

Rules:
- Every step: "- " then an optional short cue, then a duration (30s, 7m, 1m30), then ONE target.
- Targets: {target_rules}
- Repeats: a header line ending in "Nx" (e.g. "Main set 3x" or "Strides 6x") followed by its steps; a blank
  line ends the block. Never write "NxM" anywhere else, not even in cues.
- No absolute bpm or pace targets - convert them to the zone as described. You may keep the athlete's
  numbers in the cue text (e.g. "- Threshold <their bpm range> 7m Z4 HR").
- Strides / sprints count as their own repeat block.

Answer with JSON only: {{"name": "<short workout name, max 40 chars>", "workout": "<the workout text>"}}"""

DUR_RE = re.compile(r"(?<![\w.-])(?:(\d+)h)?(?:(\d+)m(?!in)(?:(\d+)s?)?)?(?:(\d+)s)?(?![\w%])")
TARGET_RE = re.compile(r"(\d+(?:-\d+)?%(?:\s*(?:LTHR|HR|MMP|Pace))?|\bZ\d(?:\s*(?:HR|Pace))?\b|\d+(?:-\d+)?w\b|\bRamp\b)",
                       re.IGNORECASE)
REPS_IN_TEXT = re.compile(r"(\d+)\s*[x×]\s*(\d+(?:[.,]5)?)\s*(s|sec|secs|seconds|min|mins|minutes|m)\b", re.IGNORECASE)
TOTAL_IN_TEXT = re.compile(r"(?<![x×\d])\s*~?(\d+)(?:\s*-\s*(\d+))?\s*min", re.IGNORECASE)


def _step_seconds(line: str) -> int | None:
    for m in DUR_RE.finditer(line):
        h, mi, msec, sec = m.groups()
        if any((h, mi, sec)):
            return int(h or 0) * 3600 + int(mi or 0) * 60 + int(msec or 0) + int(sec or 0)
    return None


def parse_check(text: str) -> tuple[int, list[tuple[int, list[int]]], list[str]]:
    """(total seconds, [(reps, [step seconds])], problems) for Intervals workout text."""
    blocks, problems, reps, steps = [], [], 1, []

    def close():
        nonlocal reps, steps
        if steps:
            blocks.append((reps, steps))
        reps, steps = 1, []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            close()
        elif line.startswith("-"):
            sec = _step_seconds(line[1:])
            if not sec:
                problems.append(f"no duration: {line}")
            elif not TARGET_RE.search(line):
                problems.append(f"no target: {line}")
            else:
                steps.append(sec)
        else:
            close()
            m = re.search(r"(\d+)\s*x\s*$", line, re.IGNORECASE)
            reps = int(m.group(1)) if m else 1
            if re.search(r"\d+\s*x\s*\d", line, re.IGNORECASE):
                problems.append(f"stray NxM in a text line: {line}")
    close()
    if not blocks:
        problems.append("no steps")
    return sum(r * sum(st) for r, st in blocks), blocks, problems


def check_pinned(athlete_text: str, workout: str) -> tuple[int, list[str]]:
    """Fidelity checks of a converted pinned session: (total seconds, problems)."""
    total, blocks, problems = parse_check(workout)
    for n, val, unit in REPS_IN_TEXT.findall(athlete_text):
        sec = round(float(val.replace(",", ".")) * (1 if unit.lower().startswith("s") else 60))
        if not any(r == int(n) and sec in st for r, st in blocks):
            problems.append(f"{n}x{val} {unit} from the athlete's text is missing")
    totals = [(int(a), int(b or a)) for a, b in TOTAL_IN_TEXT.findall(REPS_IN_TEXT.sub(" ", athlete_text))
              if int(a) >= 25]
    if totals:
        lo, hi = totals[0]
        if not lo * 60 * 0.8 <= total <= hi * 60 * 1.2:
            problems.append(f"total {total // 60} min doesn't fit the stated {lo}-{hi} min")
    if not 10 * 60 <= total <= 6 * 3600:
        problems.append(f"implausible total {total // 60} min")
    return total, problems


def _target_text(sport: str) -> tuple[str, dict]:
    from core.schedule import _targets
    t = _targets()
    hr = t["hr"]
    zones = (f"Z1 up to {hr[0]} bpm, Z2 {hr[0] + 1}-{hr[1]}, Z3 {hr[1] + 1}-{hr[2]}, Z4 {hr[2] + 1}-{hr[3]}, "
             f"Z5 above {hr[3]}")
    if sport == "ride":
        ftp = f"FTP {t['ftp']:.0f} W" if t["ftp"] else "FTP unknown"
        return (f"{ftp}. Run/bike heart-rate zones: {zones}.",
                {"easy": "55-70%", "hard": "95-105%", "rest": "50%",
                 "target_rules": "power as % of FTP (\"88-94%\", \"Ramp 50-75%\"; convert watts with the FTP above), "
                                 "optional cadence after it (\"90rpm\"); heart-rate steps as \"Z2 HR\"."})
    return (f"Run heart-rate zones: {zones}.",
            {"easy": "Z2 HR", "hard": "Z4 HR", "rest": "Z1 HR",
             "target_rules": "heart-rate zone \"Z1 HR\" .. \"Z5 HR\" - pick the zone that contains the athlete's bpm "
                             "(for a range across two zones, the zone holding most of it); strides and sprints \"Z5 HR\"; "
                             "easy/conversational \"Z2 HR\"; walking or jog recoveries \"Z1 HR\"."})


def _pin_cache() -> dict:
    try:
        with open(PIN_CACHE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def pinned_steps(text: str, sport: str) -> dict | None:
    """{"name", "workout", "minutes"} for a pinned session, or None (plain text then)."""
    targets, fmt = _target_text(sport)
    key = hashlib.sha256(f"{sport}|{text}|{targets}".encode()).hexdigest()[:16]
    cache = _pin_cache()
    hit = cache.get(key)
    if hit and hit.get("workout"):
        return hit
    if hit and datetime.now().timestamp() - hit.get("failed_at", 0) < PIN_RETRY_SEC:
        return None
    result = None
    try:
        from core.llm import gemini_llm
        content = gemini_llm("workout_converter", temperature=0).invoke(
            PIN_PROMPT.format(sport=sport, text=text, targets=targets, **fmt)).content
        if isinstance(content, list):
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        raw = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", str(content).strip())
        data = json.loads(raw)
        workout = str(data["workout"]).strip()
        total, problems = check_pinned(text, workout)
        if problems:
            logger.warning(f"Pinned session conversion rejected ({'; '.join(problems)}): {text!r}")
        else:
            result = {"name": str(data.get("name") or text)[:40], "workout": workout, "minutes": round(total / 60)}
    except Exception as e:
        logger.warning(f"Pinned session conversion failed: {e}")
    cache = _pin_cache()
    cache[key] = result or {"failed_at": datetime.now().timestamp()}
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = PIN_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dict(list(cache.items())[-100:]), f, indent=1)
    os.replace(tmp, PIN_CACHE)
    return result


# --- one session -> one workout ---

def build_workout(s: dict, day: dict, week: dict, readiness: str) -> dict:
    sport, k = s["sport"], s["kind"]
    wk = week.get("kind") or ""
    deload = bool(week.get("deload"))
    max_min = day.get("max_minutes")
    amber = readiness == "amber" and s["hard"] and sport != "gym"
    notes = [s["note"]] if s.get("note") else []
    conv = pinned_steps(s["detail"], sport) if s["pinned"] and sport != "gym" else None
    if conv:
        text, name, minutes, structured = conv["workout"], conv["name"], conv["minutes"], True
    elif s["pinned"]:
        text, name, minutes, structured = s["detail"], s["detail"][:60], None, False
    elif sport == "gym":
        text, name, structured = s["detail"], s["title"], False
        minutes = 30 if deload and k == "gym_legs" else GYM_MINUTES.get(k, 30)
    else:
        name, blocks = (_ride_blocks if sport == "ride" else _run_blocks)(s, wk, deload, amber, max_min)
        text, minutes, structured = _render(blocks), round(_total(blocks) / 60), True
        if amber:
            notes.append(WARMUP_TEST)
    if sport == "ride" and structured:
        text = scale_percent(text, _ftp_scale())
    hard_long = bool(re.search(r"sweet spot|all-out|threshold", s["detail"] or "", re.I))
    notes.append(fuel(sport, minutes, bool(s["hard"]) or hard_long, _weight())["text"])
    header = "\n".join(notes)
    description = (header + "\n\n" if header else "") + text
    return {
        "kind": k, "sport": sport, "title": s["title"],
        "name": f"{name} (AI coach)" if structured or not s["pinned"] else name,
        "type": EVENT_TYPE[sport], "description": description, "plan_detail": s["detail"], "pinned": s["pinned"],
        "moving_time_sec": minutes * 60 if minutes else None, "structured": structured,
    }


# --- FTP: Intervals.icu reads % of its own set FTP; the plan trains on the training FTP ---

PERCENT_RE = re.compile(r"(\d+)(?:-(\d+))?%")


def _weight() -> float | None:
    """The athlete's weight (kg) for the fueling line; None when unknown (a default is used)."""
    from core.overview import cached

    def compute():
        from core.db import get_connection
        from core.metrics import load_athlete_profile
        conn = get_connection()
        try:
            return load_athlete_profile(conn).get("weight_kg")
        finally:
            conn.close()
    try:
        w = cached("workout_weight", 3600, compute)
        return float(w) if w else None
    except Exception as e:
        logger.warning(f"Weight unavailable for fueling, using the default: {e}")
        return None


def _ftp_scale() -> float:
    """Training FTP / Intervals.icu's set FTP (core/metrics.training_ftp: eFTP once the tested FTP
    is over 8 weeks old), so the % targets on the watch match the plan's watts. 1.0 when unknown."""
    from core.overview import cached

    def compute():
        from core.db import get_connection
        from core.metrics import load_athlete_profile
        conn = get_connection()
        try:
            p = load_athlete_profile(conn)
        finally:
            conn.close()
        if p.get("ftp_source") == "eftp" and p.get("ftp") and p.get("ftp_set"):
            return float(p["ftp"]) / float(p["ftp_set"])
        return 1.0
    try:
        return cached("workout_ftp_scale", 300, compute)
    except Exception as e:
        logger.warning(f"FTP scale unavailable, using Intervals' FTP as it is: {e}")
        return 1.0


def scale_percent(text: str, scale: float) -> str:
    """Every "88-94%" / "50%" in a ride workout times `scale`, rounded to whole percent."""
    if abs(scale - 1) < 0.005:
        return text
    def one(m):
        lo = round(int(m.group(1)) * scale)
        return f"{lo}-{round(int(m.group(2)) * scale)}%" if m.group(2) else f"{lo}%"
    return PERCENT_RE.sub(one, text)


# --- state ---

def _path(d: date) -> str:
    return os.path.join(STATE_DIR, f"{d.isoformat()}.json")


def _load(d: date) -> dict:
    try:
        with open(_path(d)) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"date": d.isoformat(), "workouts": {}}


def _save(state: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = _path(date.fromisoformat(state["date"]))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)
    cutoff = date.today() - timedelta(days=KEEP_DAYS)
    for name in os.listdir(STATE_DIR):
        try:
            if date.fromisoformat(name[:10]) < cutoff:
                os.remove(os.path.join(STATE_DIR, name))
        except (ValueError, OSError):
            pass


def _plan_day(d: date | None) -> tuple[dict | None, dict]:
    plan = build_schedule()
    days = plan["days"]
    if d:
        return next((x for x in days if x["date"] == d.isoformat()), None), plan
    return next((x for x in days if x["sessions"]), days[0] if days else None), plan


def for_day(d: date | None = None) -> dict:
    """The workouts for day `d` (default: the plan's first day with training), merged with
    what was pushed/skipped before."""
    day, plan = _plan_day(d)
    if not day:
        return {"date": (d or datetime.now(TZ).date()).isoformat(), "status": "outside plan", "workouts": []}
    the_day = date.fromisoformat(day["date"])
    monday = (the_day - timedelta(days=the_day.weekday())).isoformat()
    week = next((w for w in plan["weeks"] if w["monday"] == monday), {})
    week = {**week, "kind": _week_kind(the_day - timedelta(days=the_day.weekday()))[0]}

    state = _load(the_day)
    stored = state["workouts"]
    current = {}
    for s in day["sessions"]:
        w = build_workout(s, day, week, plan["readiness"])
        key = w["kind"]
        prev = stored.get(key, {})
        w["status"] = prev.get("status", "draft")
        w["event_id"] = prev.get("event_id")
        w["pushed_at"] = prev.get("pushed_at")
        # Changed = the plan's prescription changed since the push (not the text: a pushed
        # workout may have been hand-structured, e.g. a pinned session).
        w["changed"] = w["status"] == "pushed" and prev.get("plan_detail") != w["plan_detail"]
        if w["status"] == "pushed" and not w["changed"]:   # show what is actually on Intervals
            w.update({k: prev[k] for k in ("name", "description", "moving_time_sec", "structured") if k in prev})
        w["in_plan"] = True
        current[key] = w
    for key, prev in stored.items():   # pushed sessions the plan no longer has
        if key not in current and prev.get("status") == "pushed":
            current[key] = {**prev, "in_plan": False, "changed": False}
    return {"date": the_day.isoformat(), "weekday": day["weekday"], "status": day["status"],
            "readiness": plan["readiness"], "workouts": list(current.values())}


def _stored_copy(w: dict) -> dict:
    return {k: w.get(k) for k in ("kind", "sport", "title", "name", "type", "description", "plan_detail", "pinned",
                                  "moving_time_sec", "structured", "status", "event_id", "pushed_at")}


# --- Intervals.icu ---

def _api(method: str, path: str, **kw):
    env = load_env()
    resp = requests.request(method, f"{BASE_URL}/api/v1/athlete/{env['INTERVALS_ATHLETE_ID']}{path}",
                            auth=("API_KEY", env["INTERVALS_API_KEY"]), timeout=30, **kw)
    return resp


def _event(d: str, w: dict) -> dict:
    body = {"category": "WORKOUT", "start_date_local": f"{d}T00:00:00", "type": w["type"], "name": w["name"],
            "description": w["description"], "external_id": f"aicoach-{d}-{w['kind']}"}
    if w.get("moving_time_sec"):
        body["moving_time"] = w["moving_time_sec"]
    return body


def _find(d: str | None, kind: str) -> tuple[dict, dict]:
    day = for_day(date.fromisoformat(d) if d else None)
    w = next((x for x in day["workouts"] if x["kind"] == kind), None)
    if not w:
        raise WorkoutError(f"No {kind} workout on {day['date']} any more - the plan changed.")
    return day, w


def push(d: str | None, kind: str) -> dict:
    """Creates (or updates) the workout on the Intervals.icu calendar."""
    day, w = _find(d, kind)
    if not w["in_plan"]:
        raise WorkoutError("That session is no longer in the plan - remove it instead.")
    body = _event(day["date"], w)
    resp = None
    if w.get("event_id"):
        resp = _api("PUT", f"/events/{w['event_id']}", json=body)
        if resp.status_code == 404:
            resp = None
    if resp is None:
        resp = _api("POST", "/events", params={"upsertOnUid": "false"}, json=body)
    if resp.status_code >= 300:
        raise WorkoutError(f"Intervals.icu said {resp.status_code}: {resp.text[:200]}")
    event = resp.json()
    w.update(status="pushed", event_id=event.get("id"), pushed_at=datetime.now(TZ).isoformat(timespec="minutes"),
             changed=False)
    state = _load(date.fromisoformat(day["date"]))
    state["workouts"][kind] = _stored_copy(w)
    _save(state)
    logger.info(f"Pushed {kind} for {day['date']} to Intervals.icu (event {w['event_id']}).")
    return {**w, "date": day["date"]}


def skip(d: str | None, kind: str) -> dict:
    day, w = _find(d, kind)
    if w["status"] == "pushed":
        raise WorkoutError("Already on Intervals.icu - use Remove.")
    w["status"] = "skipped"
    state = _load(date.fromisoformat(day["date"]))
    state["workouts"][kind] = _stored_copy(w)
    _save(state)
    return {**w, "date": day["date"]}


def remove(d: str | None, kind: str) -> dict:
    """Deletes the pushed workout from the Intervals.icu calendar."""
    day, w = _find(d, kind)
    if not w.get("event_id"):
        raise WorkoutError("Not on Intervals.icu.")
    resp = _api("DELETE", f"/events/{w['event_id']}")
    if resp.status_code >= 300 and resp.status_code != 404:
        raise WorkoutError(f"Intervals.icu said {resp.status_code}: {resp.text[:200]}")
    state = _load(date.fromisoformat(day["date"]))
    if w["in_plan"]:
        w.update(status="skipped", event_id=None, pushed_at=None)
        state["workouts"][kind] = _stored_copy(w)
    else:
        state["workouts"].pop(kind, None)
    _save(state)
    logger.info(f"Removed {kind} for {day['date']} from Intervals.icu.")
    return {**w, "date": day["date"]}


def act(d: str | None, kind: str, action: str) -> dict:
    fn = {"push": push, "skip": skip, "remove": remove}.get(action)
    if not fn:
        raise WorkoutError(f"Unknown action {action!r}.")
    return fn(d, kind)


# --- planned vs done: Intervals.icu pairs an activity with the planned workout of its day ---

COMPLETED_PCT = 80   # compliance at or above this counts the planned session as done


def pushed_events() -> dict:
    """{Intervals event id: {date, kind, name, description}} for every workout pushed and kept
    (the last KEEP_DAYS days)."""
    out = {}
    try:
        names = os.listdir(STATE_DIR)
    except FileNotFoundError:
        return out
    for n in names:
        if not re.match(r"\d{4}-\d{2}-\d{2}\.json$", n):
            continue
        try:
            with open(os.path.join(STATE_DIR, n)) as f:
                state = json.load(f)
        except (OSError, ValueError):
            continue
        for kind, w in state.get("workouts", {}).items():
            if w.get("event_id") and w.get("status") == "pushed":
                out[int(w["event_id"])] = {"date": state["date"], "kind": kind, "name": w.get("name"),
                                           "description": w.get("description")}
    return out


def pct(compliance) -> float | None:
    """Intervals.icu compliance as a percentage."""
    try:
        return round(float(compliance), 1)
    except (TypeError, ValueError):
        return None


def match(paired_event_id, compliance, events: dict | None = None) -> dict | None:
    """The pushed workout an activity was paired with, plus its compliance %, or None."""
    try:
        ev = (events if events is not None else pushed_events()).get(int(paired_event_id))
    except (TypeError, ValueError):
        return None
    if not ev:
        return None
    c = pct(compliance)
    return {**ev, "compliance": c, "completed": c is not None and c >= COMPLETED_PCT}


def telegram_text(day: dict, w: dict) -> str:
    when = f"{day['weekday']} {day['date'][8:]}.{day['date'][5:7]}."
    mins = f" · {w['moving_time_sec'] // 60} min" if w.get("moving_time_sec") else ""
    state = {"pushed": "✅ On Intervals.icu" + (" (plan changed since - Push updates it)" if w.get("changed") else ""),
             "skipped": "✖ Skipped"}.get(w["status"], "")
    if not w.get("in_plan", True):
        state = "⚠️ On Intervals.icu but no longer in the plan"
    yours = f"\n\nYour session: {w['plan_detail']}" if w.get("pinned") and w.get("structured") else ""
    return (f"🗓 Workout for {when}: {w['name']}{mins}{yours}\n\n{w['description']}"
            + (f"\n\n{state}" if state else ""))
