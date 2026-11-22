"""
Structured workouts from the rolling 7-day plan (core/schedule.py), pushed to the
Intervals.icu calendar only when the athlete says so.

Every morning the Telegram bot builds the workouts for the plan's first day (after the
briefing) and asks Push / Skip for each; the dashboard shows the same workouts with the same
buttons. Pushing creates a planned WORKOUT event on Intervals.icu from the workout text below
(Intervals parses it into steps, so it syncs on to Garmin/Zwift like any planned workout and
can be downloaded as .zwo/.fit/.mrc from there).

Workout text (Intervals.icu workout builder syntax):
- rides use % of FTP (Intervals' own FTP setting), runs use Intervals' run HR zones ("Z2 HR"),
- gym sessions become WeightTraining events with the prescription as text, no steps,
- a pinned session (the athlete's own words) is pushed as text - Intervals parses it if it
  happens to be in workout syntax,
- amber readiness drops one rep of the main set and adds the warm-up test; a "max N min" day
  shortens warm-up/cool-down first, then drops reps; red readiness is already short and easy
  in the plan.

The state lives in state/workouts/<date>.json: per session the last pushed text, the event id
on Intervals and the status (draft / pushed / skipped). The workout is rebuilt from the plan on
every read; when it no longer matches what was pushed, `changed` is set and Push updates the
same event. A pushed session the plan no longer has (done already, moved) is kept with
`in_plan: false` so it can still be removed.
"""
import json
import logging
import os
import re
from datetime import date, datetime, timedelta

import requests

from core.db import load_env
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


def _steady(minutes: int, target: str, wu_target: str, max_min: int | None, cue: str = "") -> list:
    minutes = min(minutes, max_min) if max_min else minutes
    if minutes < 30:
        return [("Main set", 1, [(minutes * 60, target, cue)])]
    wu, cd = (10, 5) if minutes < 90 else (15, 10)
    return [("Warmup", 1, [(wu * 60, wu_target, "")]),
            ("Main set", 1, [((minutes - wu - cd) * 60, target, cue)]),
            ("Cooldown", 1, [(cd * 60, wu_target, "")])]


def _ride_blocks(s: dict, wk: str, deload: bool, amber: bool, max_min: int | None) -> tuple[str, list]:
    k, detail = s["kind"], s["detail"]
    if k == "bike_q":
        args = dict(wu_target="Ramp 50-75% 90rpm", cd_target="50%", amber=amber, max_min=max_min)
        if detail.startswith("VO2max"):
            on = 240 if "5x4" in detail else 180
            return (f"VO2max 5x{on // 60} min",
                    _interval_session(900, [(on, "106-120% 95rpm", ""), (on, "50%", "Easy")], 5, 600, min_reps=3, **args))
        if detail.startswith("Tempo"):
            return ("Tempo 2x15 min",
                    _interval_session(900, [(900, "76-87%", ""), (300, "55%", "Easy")], 2, 600,
                                      extra=("Endurance", 1, [(600, "62-72%", "")]), min_reps=1, **args))
        if detail.startswith("Sweet spot 3x15"):
            return ("Sweet spot 3x15 min", _interval_session(900, [(900, "88-94%", ""), (300, "55%", "Easy")], 3, 600, **args))
        if detail.startswith("Threshold"):
            return ("Threshold 3x10 min", _interval_session(900, [(600, "95-105%", ""), (300, "55%", "Easy")], 3, 600, **args))
        return ("Sweet spot 3x12 min", _interval_session(900, [(720, "88-94%", ""), (300, "55%", "Easy")], 3, 600, **args))
    if k == "long_ride":
        mins = 80 if deload else 135
        return (f"Long ride {mins} min Z2", _steady(mins, "60-72% 85-95rpm", "50-60%", max_min, "Fuel 60 g carbs/h"))
    # easy ride
    if deload:
        return ("Easy ride 50 min", _steady(50, "50-68%", "50%", max_min))
    if "sprints" in detail:
        mins = min(65, max_min or 65)
        return ("Endurance + sprints", [
            ("Warmup", 1, [(900, "Ramp 50-70% 90rpm", "")]),
            ("Sprints", 7, [(15, "150%", "Sprint"), (165, "55%", "Easy")]),
            ("Endurance", 1, [(max(300, mins * 60 - 900 - 1260 - 600), "62-72% 85-95rpm", "")]),
            ("Cooldown", 1, [(600, "50%", "")])])
    if "2 min hard" in detail:
        mins = min(70, max_min or 70)
        return ("Endurance + 5x2 min", [
            ("Warmup", 1, [(900, "Ramp 50-70% 90rpm", "")]),
            ("Hard efforts", 5, [(120, "106-115%", ""), (180, "55%", "Easy")]),
            ("Endurance", 1, [(max(300, mins * 60 - 900 - 1500 - 600), "62-72% 85-95rpm", "")]),
            ("Cooldown", 1, [(600, "50%", "")])])
    return ("Endurance ride 65 min", _steady(65, "62-72% 85-95rpm", "50-60%", max_min))


def _run_blocks(s: dict, wk: str, deload: bool, amber: bool, max_min: int | None) -> tuple[str, list]:
    k, detail = s["kind"], s["detail"]
    if k == "run_q" and not detail.startswith("Easy run"):
        strides = ("Strides", 4, [(20, "Z5 HR", "Stride"), (40, "Z1 HR", "Walk/jog")])
        args = dict(wu_target="Z2 HR", cd_target="Z1 HR", amber=amber, max_min=max_min)
        if detail.startswith("Tempo"):
            b = _interval_session(600, [(720, "Z3 HR", "Tempo"), (180, "Z1 HR", "Jog")], 2, 480, min_reps=1, **args)
            name = "Tempo run 2x12 min"
        elif detail.startswith("Hills"):
            b = _interval_session(600, [(60, "Z5 HR", "Hard uphill"), (90, "Z1 HR", "Jog down")], 8, 480, min_reps=5, **args)
            name = "Hill repeats 8x60 s"
        else:
            b = _interval_session(600, [(480, "Z4 HR", "Threshold"), (120, "Z1 HR", "Jog")], 3, 480, **args)
            name = "Threshold run 3x8 min"
        return name, b[:1] + [strides] + b[1:]
    m = re.match(r"(?:Easy run )?(\d+)", detail)
    if k == "long_run" and not detail.startswith("Easy run"):
        mins = 45 if deload else 65
        return f"Long run {mins} min easy", _steady(mins, "Z2 HR", "Z1 HR", max_min, "Conversational")
    mins = int(m.group(1)) if m else 40
    mins = 40 if mins == 35 else mins          # "35-45 min" -> 40
    b = _steady(mins, "Z2 HR", "Z1 HR", max_min)
    if "strides" in detail:
        b.append(("Strides", 6, [(20, "Z5 HR", "Stride"), (60, "Z1 HR", "Walk/jog")]))
    return f"Easy run {mins} min", b


# --- one session -> one workout ---

def build_workout(s: dict, day: dict, week: dict, readiness: str) -> dict:
    sport, k = s["sport"], s["kind"]
    wk = week.get("kind") or ""
    deload = bool(week.get("deload"))
    max_min = day.get("max_minutes")
    amber = readiness == "amber" and s["hard"] and sport != "gym"
    notes = [s["note"]] if s.get("note") else []
    if s["pinned"]:
        text, name, minutes, structured = s["detail"], s["detail"][:60], None, False
    elif sport == "gym":
        text, name, structured = s["detail"], s["title"], False
        minutes = 30 if deload and k == "gym_legs" else GYM_MINUTES.get(k, 30)
    else:
        name, blocks = (_ride_blocks if sport == "ride" else _run_blocks)(s, wk, deload, amber, max_min)
        text, minutes, structured = _render(blocks), round(_total(blocks) / 60), True
        if amber:
            notes.append(WARMUP_TEST)
    header = "\n".join(notes)
    description = (header + "\n\n" if header else "") + text
    return {
        "kind": k, "sport": sport, "title": s["title"],
        "name": f"{name}" if s["pinned"] else f"{name} (AI coach)",
        "type": EVENT_TYPE[sport], "description": description, "plan_detail": s["detail"],
        "moving_time_sec": minutes * 60 if minutes else None, "structured": structured,
    }


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
        w["changed"] = w["status"] == "pushed" and (prev.get("description"), prev.get("name")) != (w["description"], w["name"])
        w["in_plan"] = True
        current[key] = w
    for key, prev in stored.items():   # pushed sessions the plan no longer has
        if key not in current and prev.get("status") == "pushed":
            current[key] = {**prev, "in_plan": False, "changed": False}
    return {"date": the_day.isoformat(), "weekday": day["weekday"], "status": day["status"],
            "readiness": plan["readiness"], "workouts": list(current.values())}


def _stored_copy(w: dict) -> dict:
    return {k: w.get(k) for k in ("kind", "sport", "title", "name", "type", "description", "plan_detail",
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


def telegram_text(day: dict, w: dict) -> str:
    when = f"{day['weekday']} {day['date'][8:]}.{day['date'][5:7]}."
    mins = f" · {w['moving_time_sec'] // 60} min" if w.get("moving_time_sec") else ""
    state = {"pushed": "✅ On Intervals.icu" + (" (plan changed since - Push updates it)" if w.get("changed") else ""),
             "skipped": "✖ Skipped"}.get(w["status"], "")
    if not w.get("in_plan", True):
        state = "⚠️ On Intervals.icu but no longer in the plan"
    return (f"🗓 Workout for {when}: {w['name']}{mins}\n\n{w['description']}"
            + (f"\n\n{state}" if state else ""))
