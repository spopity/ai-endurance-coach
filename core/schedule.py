"""
The rolling 7-day plan: what to do on each of the next 7 days, laid out in code (no LLM) from
the week's planned session mix (training_blocks + the training focus), what was already done
this week, the availability file (days off, limited days, pinned sessions), readiness and the
hard/easy rules.

It is stateless: every read re-derives the week from what was actually done, so it adapts by
itself after every sync. A skipped hard ride is still "to do" and moves to the next day that
isn't next to another hard day; a hard ride cut short still counts when it reached the
quality threshold (core/plan.classify) and otherwise stays to do; when the days run out, the
filler goes first (an easy ride, then an easy run, then the third gym session) and the key
sessions stay (bike quality, run quality, long ride, long run). Nothing is ever doubled up to
"make up" a missed session.

Rules for the layout (the dual-sport week in core/focus.py):
- at most one endurance session a day; gym can share a day,
- hard sessions (bike quality, run quality, heavy legs gym) never on consecutive days - also
  not the day after yesterday's hard session,
- heavy legs gym goes on a hard day, after the quality session,
- the long ride and long run prefer the weekend and need a day with enough time,
- day 1 follows readiness: red -> nothing hard; amber -> the hard session is trimmed ~20% with
  the warm-up test; running limits (core/readiness.running_limit) turn quality runs easy.

Sessions get concrete targets from the training FTP and the heart-rate zones of the last run.
The head coach's next session is day 1 of this plan (agents/ai_coach.py rule 19).
"""
import itertools
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core import availability
from core.focus import get_focus
from core.gym import LEG_WORDS, WEIGHTS_TYPES
from core.overview import cached, query
from core.plan import LONG_RIDE_MIN, RIDE_TYPES, RUN_TYPES, classify, run_kind, week_block

logger = logging.getLogger("AI_COACH_V1.schedule")

TZ = ZoneInfo("Europe/Bratislava")
DAYS = 7
WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

HARD = {"bike_q", "run_q", "gym_legs"}
ENDURANCE = {"bike_q", "long_ride", "ride", "run_q", "long_run", "run"}
SPORT = {"bike_q": "ride", "long_ride": "ride", "ride": "ride", "run_q": "run", "long_run": "run", "run": "run",
         "gym_legs": "gym", "gym_upper": "gym", "gym_core": "gym"}
# Minutes a day needs for the session (limited days).
NEEDS = {"bike_q": 45, "run_q": 35, "long_ride": 90, "long_run": 50, "ride": 30, "run": 25}
# Key sessions, most important first, and how much keeping each one is worth to the layout.
KEY_WEIGHT = {"bike_q": 100, "run_q": 90, "long_ride": 80, "long_run": 70}
SECOND_BIKE_Q_WEIGHT = 60   # a second bike quality session ranks below one quality session per sport
QUALITY_PHASES = {"Build", "Race prep"}
TITLES = {"bike_q": "Bike quality", "long_ride": "Long ride", "ride": "Easy ride", "run_q": "Quality run",
          "long_run": "Long run", "run": "Easy run", "gym_legs": "Gym - heavy legs", "gym_upper": "Gym - upper body + core",
          "gym_core": "Gym - core + mobility"}


# --- what the week asks for and what's done ---

def _week_kind(monday: date) -> tuple[str | None, dict | None]:
    """(periodization kind like 'build_2', the training_blocks row with the focus applied)."""
    from core.periodization import FOCUS
    rows = query("SELECT primary_focus FROM training_blocks WHERE start_date <= %s AND end_date >= %s "
                 "ORDER BY start_date LIMIT 1;", (monday + timedelta(days=3), monday + timedelta(days=3)))
    kind = next((k for k, text in FOCUS.items() if rows and rows[0]["primary_focus"] == text), None)
    return kind, week_block(monday)


def _done(start: date, end: date) -> list[dict]:
    """Training between start and end (local days): kind, day and whether it was hard."""
    rows = query("""
        SELECT (start_time AT TIME ZONE %s)::date AS day, type, name, moving_time_sec,
               raw_payload->'icu_zone_times' AS pz, raw_payload->'icu_hr_zone_times' AS hz
        FROM activities WHERE superseded_by IS NULL
          AND (start_time AT TIME ZONE %s)::date BETWEEN %s AND %s ORDER BY start_time;
    """, (str(TZ), str(TZ), start, end))
    out = []
    for r in rows:
        mins = (r["moving_time_sec"] or 0) / 60
        if r["type"] in RUN_TYPES:
            k = {"quality": "run_q", "long": "long_run", "easy": "run"}[run_kind(r["hz"], r["moving_time_sec"])]
        elif r["type"] in RIDE_TYPES:
            c = classify(r["type"], r["pz"], r["hz"])
            k = "bike_q" if c in ("sst", "vo2") else "long_ride" if mins >= LONG_RIDE_MIN else "ride"
            if c == "vo2":
                out.append({"day": r["day"], "kind": k, "flavor": "vo2", "name": r["name"], "minutes": round(mins)})
                continue
        elif r["type"] in WEIGHTS_TYPES:
            k = "gym_legs" if LEG_WORDS.search(r["name"] or "") else "gym_upper"
        else:
            continue
        out.append({"day": r["day"], "kind": k, "name": r["name"], "minutes": round(mins)})
    return out


def _todo(monday: date, done: list[dict]) -> tuple[dict, list[str]]:
    """(week info, sessions still to do this week, most important first)."""
    kind, block = _week_kind(monday)
    if not block or not block.get("sessions"):
        return {"kind": kind, "phase": None, "deload": False}, []
    s = {k: int(v or 0) for k, v in block["sessions"].items()}
    phase, deload = block["phase"], bool(block["is_deload"])
    info = {"kind": kind, "phase": phase, "deload": deload, "focus": block["primary_focus"],
            "mix": {k: v for k, v in s.items() if v},
            "vo2_left": max(0, s.get("vo2", 0) - sum(1 for d in done if d.get("flavor") == "vo2"))}
    n = {k: sum(1 for d in done if d["kind"] == k) for k in TITLES}
    rides_left = max(0, s.get("endurance", 0) + s.get("sst", 0) + s.get("vo2", 0)
                     - n["bike_q"] - n["long_ride"] - n["ride"])
    runs_left = max(0, s.get("run", 0) - n["run_q"] - n["long_run"] - n["run"])
    gym_left = max(0, s.get("weights", 0) - n["gym_legs"] - n["gym_upper"])

    rides, runs, gym = [], [], []
    for _ in range(min(rides_left, max(0, s.get("sst", 0) + s.get("vo2", 0) - n["bike_q"]))):
        rides.append("bike_q")
    if len(rides) < rides_left and not n["long_ride"] and s.get("endurance", 0):
        rides.append("long_ride")
    rides += ["ride"] * (rides_left - len(rides))
    if runs_left and not n["run_q"] and phase in QUALITY_PHASES and not deload:
        runs.append("run_q")
    if len(runs) < runs_left and not n["long_run"]:
        runs.append("long_run")
    runs += ["run"] * (runs_left - len(runs))
    if gym_left and not n["gym_legs"]:
        gym.append("gym_legs")
    gym += ["gym_upper" if not n["gym_upper"] and "gym_upper" not in gym else "gym_core"
            for _ in range(gym_left - len(gym))]
    # Filler order (dropped first when days run out) is the reverse of this list.
    keys = [k for k in ("bike_q", "run_q", "long_ride", "long_run") for _ in range(rides.count(k) + runs.count(k))]
    easy = [k for pair in itertools.zip_longest([r for r in runs if r == "run"], [r for r in rides if r == "ride"])
            for k in pair if k]
    return info, keys + easy + gym


# --- the days ---

PIN_RULES = [  # (pattern, kind) for sessions the athlete pinned in chat
    (r"gym|weights?|strength|squat|deadlift", None),
    (r"\brun|jog", "run"),
    (r"ride|bike|cycling|spin|zwift|trainingpeaks|gravel|mtb", "ride"),
]
PIN_HARD = re.compile(r"threshold|tempo|vo2|sweet ?spot|interval|hills?\b|repeats|\d+\s*x\s*\d+", re.IGNORECASE)
PIN_LONG = re.compile(r"\blong\b", re.IGNORECASE)


def pin_kind(text: str) -> str:
    t = text.lower()
    sport = next((k for pattern, k in PIN_RULES if re.search(pattern, t)), "ride")
    if sport is None:
        return "gym_legs" if LEG_WORDS.search(t) else "gym_upper"
    if PIN_HARD.search(t):
        return "run_q" if sport == "run" else "bike_q"
    if PIN_LONG.search(t):
        return "long_run" if sport == "run" else "long_ride"
    return sport


def _days(start: date, avail: dict, n: int = DAYS) -> list[dict]:
    out = []
    for i in range(n):
        d = start + timedelta(days=i)
        st = availability.day_status(d, avail)
        pin = availability.planned_session(d, avail)
        kind = st["kind"] if st else "available"
        out.append({"date": d, "status": kind, "max_minutes": st.get("max_minutes") if st else None,
                    "off": kind in availability.NO_TRAINING, "pin": pin and pin["session"],
                    "sessions": []})
    return out


def _fits(kind: str, day: dict) -> bool:
    return not day["off"] and (day["max_minutes"] is None or day["max_minutes"] >= NEEDS.get(kind, 0))


def _place_keys(keys: list[str], days: list[dict], hard_before: set[date], no_hard: set[date]) -> dict:
    """Best assignment {index in keys: day index} for the key sessions (unplaced ones are dropped).
    Tiny search space (<= 5 sessions, <= 7 days), so it simply tries everything."""
    free = [i for i, d in enumerate(days) if not d["off"] and not d["sessions"]]
    best, best_score = {}, float("-inf")
    weight = [SECOND_BIKE_Q_WEIGHT if kind == "bike_q" and keys[:k].count("bike_q") else KEY_WEIGHT[kind]
              for k, kind in enumerate(keys)]

    def score(assign: dict) -> float:
        hard_days = {days[i]["date"] for k, i in assign.items() if keys[k] in HARD} | hard_before
        total = 0.0
        for k, i in assign.items():
            kind, d = keys[k], days[i]["date"]
            total += weight[k]
            if kind in HARD:
                if d - timedelta(days=1) in hard_days or d + timedelta(days=1) in hard_days or d in no_hard:
                    return float("-inf")
                total -= i * 0.5                       # earlier leaves room to move a missed one
                if any(abs((d - h).days) == 2 for h in hard_days if h != d):
                    total += 0                          # one easy day between is fine
            if kind in ("long_ride", "long_run"):
                total += 6 if d.weekday() >= 5 else 0   # weekend
                if days[i]["max_minutes"] is None:
                    total += 2
        long_days = [days[i]["date"] for k, i in assign.items() if keys[k] in ("long_ride", "long_run")]
        for d in long_days:  # don't put a hard session right after a long one
            if d + timedelta(days=1) in hard_days:
                total -= 4
        return total

    def search(k: int, assign: dict, used: set):
        nonlocal best, best_score
        if k == len(keys):
            sc = score(assign)
            if sc > best_score:
                best, best_score = dict(assign), sc
            return
        for i in free:
            if i not in used and _fits(keys[k], days[i]):
                assign[k] = i
                search(k + 1, assign, used | {i})
                del assign[k]
        search(k + 1, assign, used)   # drop it

    search(0, {}, set())
    return best


def _layout(days: list[dict], todo: list[str], hard_before: set[date], no_hard: set[date]) -> list[str]:
    """Places todo into days (mutates them); returns the dropped sessions."""
    keys = [k for k in todo if k in KEY_WEIGHT]
    easy = [k for k in todo if k in ("ride", "run")]
    gym = [k for k in todo if SPORT[k] == "gym"]
    placed = _place_keys(keys, days, hard_before, no_hard)
    dropped = [keys[k] for k in range(len(keys)) if k not in placed]
    for k, i in placed.items():
        days[i]["sessions"].append(keys[k])

    # Easy sessions: one per free day, alternating sports where possible.
    prev_sport = None
    for day in days:
        if day["off"] or any(SPORT[s] != "gym" for s in day["sessions"]) or not easy:
            prev_sport = next((SPORT[s] for s in day["sessions"] if SPORT[s] != "gym"), None)
            continue
        options = [k for k in easy if _fits(k, day)]
        if not options:
            prev_sport = None
            continue
        pick = next((k for k in options if SPORT[k] != prev_sport), options[0])
        easy.remove(pick)
        day["sessions"].append(pick)
        prev_sport = SPORT[pick]
    dropped += easy

    # Gym: heavy legs on a hard day (bike quality first), never the day before a hard session;
    # the rest on easy or rest days, one gym session a day.
    hard_days = {d["date"] for d in days if any(s in HARD for s in d["sessions"])} | hard_before
    for g in gym:
        def ok(day):
            return not day["off"] and not any(SPORT[s] == "gym" for s in day["sessions"])
        if g == "gym_legs":
            cands = [d for d in days if ok(d) and "bike_q" in d["sessions"]] or \
                    [d for d in days if ok(d) and "run_q" in d["sessions"]] or \
                    [d for d in days if ok(d) and d["date"] not in no_hard
                     and d["date"] + timedelta(days=1) not in hard_days
                     and d["date"] - timedelta(days=1) not in hard_days
                     and not any(s in ("long_ride", "long_run") for s in d["sessions"])]
        else:
            cands = [d for d in days if ok(d) and not any(s in HARD for s in d["sessions"])
                     and not any(s in ("long_ride", "long_run") for s in d["sessions"])] or \
                    [d for d in days if ok(d) and not any(s in HARD for s in d["sessions"])]
        if cands:
            cands[0]["sessions"].append(g)
            if g == "gym_legs":
                hard_days.add(cands[0]["date"])
        else:
            dropped.append(g)
    return dropped


# --- concrete targets ---

def _targets() -> dict:
    """Training FTP and the run heart-rate zones (tops of Z1..Z4) from the latest run."""
    from core.db import get_connection
    from core.metrics import load_athlete_profile
    conn = get_connection()
    try:
        ftp = load_athlete_profile(conn).get("ftp")
    finally:
        conn.close()
    rows = query("""SELECT raw_payload->'icu_hr_zones' AS z FROM activities
                    WHERE superseded_by IS NULL AND type IN ('Run', 'TrailRun') AND raw_payload ? 'icu_hr_zones'
                    ORDER BY start_time DESC LIMIT 1;""")
    hr = rows[0]["z"] if rows and rows[0]["z"] else [141, 149, 157, 166]
    return {"ftp": float(ftp) if ftp else None, "hr": [int(x) for x in hr[:4]]}


def _w(t: dict, lo: float, hi: float) -> str:
    return f"{t['ftp'] * lo:.0f}-{t['ftp'] * hi:.0f} W" if t["ftp"] else f"{lo * 100:.0f}-{hi * 100:.0f}% FTP"


def _cap(minutes: str, day: dict) -> str:
    return f"{minutes}, at most {day['max_minutes']} min today" if day["max_minutes"] else minutes


def describe(kind: str, info: dict, t: dict, day: dict, first_ride: bool = False, first_run: bool = False,
             run_limit: str = "none") -> str:
    text = _describe(kind, info, t, day, first_ride, first_run, run_limit)
    if day["max_minutes"] and kind in ("bike_q", "run_q") and "at most" not in text:
        text += (f" - only {day['max_minutes']} min today: keep the main set, shorten the warm-up and "
                 "cool-down, drop a rep if it doesn't fit")
    return text


def _describe(kind: str, info: dict, t: dict, day: dict, first_ride: bool, first_run: bool, run_limit: str) -> str:
    wk, deload, phase = info.get("kind") or "", info.get("deload"), info.get("phase")
    z2, easy_hr = _w(t, 0.56, 0.75), t["hr"][1]
    if kind == "bike_q":
        if info.get("_vo2_next"):
            reps = "5x4 min" if wk in ("build_3", "specific") else "5x3 min"
            return f"VO2max {reps} at {_w(t, 1.06, 1.20)}, equal easy recoveries; 15 min warm-up, 10 min cool-down (~65 min)"
        if wk == "base_late":
            return f"Tempo 2x15 min at {_w(t, 0.76, 0.87)}, 5 min easy between, inside a 75 min Zone 2 ride"
        if wk == "build_2":
            return f"Sweet spot 3x15 min at {_w(t, 0.88, 0.94)} (or 2x20), 5 min easy between (~90 min)"
        if wk in ("build_3", "specific"):
            return f"Threshold 3x10 min at {_w(t, 0.95, 1.05)}, 5 min easy between (~75 min)"
        return f"Sweet spot 3x12 min at {_w(t, 0.88, 0.94)}, 5 min easy between (~75 min)"
    if kind == "long_ride":
        mins = "75-90 min" if deload else "2-2.5 h"
        return f"{_cap(mins, day)} in Zone 2 ({z2}), steady, fuel 60 g carbs/h"
    if kind == "ride":
        if deload:
            return f"{_cap('45-60 min', day)} Zone 1-2 ({_w(t, 0.50, 0.70)}), relaxed"
        extra = ""
        if first_ride and wk == "base_early":
            extra = ", with 6-8 x 15 s sprints in the middle"
        elif first_ride and wk == "base_late":
            extra = f", with 5 x 2 min hard ({_w(t, 1.06, 1.15)}) in the middle"
        return f"{_cap('60-75 min', day)} Zone 2 ({z2}), 85-95 rpm{extra}"
    if kind == "run_q":
        if run_limit != "none":
            return f"Easy run 35-40 min, HR under {easy_hr} (running is limited: no hard run this week)"
        if wk == "build_1":
            main = f"Tempo 2x12 min at {t['hr'][1] + 1}-{t['hr'][2]} bpm, 3 min jog between"
        elif wk == "build_3":
            main = "Hills 8x60 s hard uphill, jog down"
        else:
            main = f"Threshold 3x8 min at {t['hr'][2] + 1}-{t['hr'][3]} bpm, 2 min jog between"
        return f"{main}; 10 min easy warm-up with strides, 8 min cool-down (~45-50 min)"
    if kind == "long_run":
        if run_limit == "easy_short":
            return f"Easy run 30 min, HR under {easy_hr} (running is limited)"
        mins = "45-50 min" if deload else "60-75 min"
        return f"{_cap(mins, day)} easy, HR under {easy_hr}, conversational"
    if kind == "run":
        mins = "30 min" if run_limit == "easy_short" else "35-45 min"
        strides = ", finish with 6 x 20 s strides" if first_run and phase == "Base" and run_limit == "none" else ""
        return f"{_cap(mins, day)} easy, HR under {easy_hr}{strides}"
    if kind == "gym_legs":
        if deload:
            return "Light full body 30 min, 2 sets, nothing heavy"
        return ("Squat or trap-bar deadlift + split squat or lunge + calf raises, 3-4 sets of 4-8 reps "
                "(2 reps in reserve), 40-45 min - after the quality session, ideally 6+ h later")
    if kind == "gym_upper":
        return "Upper body + core 30-40 min, moderate (2-3 reps in reserve)"
    return "Core + mobility 20-30 min"


# --- public API ---

def _today() -> date:
    return datetime.now(TZ).date()


def build(today: date | None = None, readiness: dict | None = None) -> dict:
    """The next 7 days. `readiness` (core/readiness) shapes day 1; fetched when not given."""
    today = today or _today()
    if readiness is None:
        try:
            from core.readiness import current_readiness
            readiness = current_readiness()
        except Exception as e:
            logger.warning(f"Readiness unavailable for the schedule: {e}")
            readiness = {}
    level = readiness.get("level", "green")
    run_limit = (readiness.get("running") or {}).get("limit", "none")
    avail = availability.load()   # housekeeping always with the real date - never a test date

    this_monday = today - timedelta(days=today.weekday())
    done_week = _done(this_monday, today)
    done_today = [d for d in done_week if d["day"] == today]
    trained_today = any(d["kind"] in ENDURANCE for d in done_today)
    start = today + timedelta(days=1) if trained_today else today
    # The last week in the window is laid out in full, so its long sessions still find the weekend;
    # only the first DAYS days are shown.
    last = start + timedelta(days=DAYS - 1)
    days = _days(start, avail, (last + timedelta(days=6 - last.weekday()) - start).days + 1)
    yesterday = start - timedelta(days=1)
    hard_before = {d["day"] for d in done_week if d["kind"] in HARD and d["day"] >= yesterday}
    if trained_today and any(d["kind"] in HARD for d in done_today):
        hard_before.add(today)
    first = next((d for d in days if not d["off"]), None)
    no_hard = {first["date"]} if first and level == "red" else set()
    if first and level == "red" and not first["pin"]:
        # Red: day 1 is a short easy session or rest - nothing hard, nothing long.
        first["max_minutes"] = min(first["max_minutes"] or 45, 45)
        first["status"] = first["status"] if first["status"] != "available" else "red readiness"

    # Pinned sessions take their day first (and a pinned hard one counts for the spacing).
    for day in days:
        if day["pin"] and not day["off"]:
            day["sessions"].append(pin_kind(day["pin"]))
            if day["sessions"][-1] in HARD:
                hard_before.add(day["date"])

    weeks, dropped = [], []
    for monday in sorted({d["date"] - timedelta(days=d["date"].weekday()) for d in days}):
        seg = [d for d in days if monday <= d["date"] < monday + timedelta(days=7)]
        done = done_week if monday == this_monday else []
        info, todo = _todo(monday, done)
        for day in seg:  # a pinned session counts towards the week
            for k in day["sessions"]:
                alt = {"bike_q": ["bike_q", "ride", "long_ride"], "run_q": ["run_q", "run", "long_run"]}.get(k, [k])
                hit = next((x for x in alt if x in todo), None) or next(
                    (x for x in todo if SPORT[x] == SPORT[k]), None)
                if hit:
                    todo.remove(hit)
        if run_limit != "none":
            todo = ["run" if k == "run_q" else k for k in todo]
        dropped += [(monday, k) for k in _layout(seg, todo, hard_before, no_hard)]
        hard_before |= {d["date"] for d in seg if any(s in HARD for s in d["sessions"])}
        info["monday"] = monday
        weeks.append(info)

    t = _targets()
    out_days, seen = [], set()
    for day in days[:DAYS]:
        monday = day["date"] - timedelta(days=day["date"].weekday())
        info = next(w for w in weeks if w["monday"] == monday)
        sessions = []
        for k in sorted(day["sessions"], key=lambda k: SPORT[k] == "gym"):
            pinned = bool(day["pin"]) and k == pin_kind(day["pin"])
            if k == "bike_q" and not pinned:
                info["_vo2_next"] = info.get("vo2_left", 0) > 0
                info["vo2_left"] = max(0, info.get("vo2_left", 0) - 1)
            first_ride = k == "ride" and (monday, "ride") not in seen
            first_run = k == "run" and (monday, "run") not in seen
            seen |= {(monday, k)}
            detail = day["pin"] if pinned else describe(k, info, t, day, first_ride, first_run, run_limit)
            note = None
            if day is first and k in HARD and SPORT[k] != "gym" and level == "amber":
                note = "amber readiness: trim it ~20% and start with the warm-up test"
            if day is first and level == "red" and not pinned:
                note = "red readiness: keep it easy and short, or rest"
            title = "Gym - light full body" if k == "gym_legs" and info.get("deload") else TITLES[k]
            sessions.append({"kind": k, "sport": SPORT[k], "title": title, "detail": detail,
                             "hard": k in HARD, "key": k in KEY_WEIGHT, "pinned": pinned, "note": note})
        out_days.append({
            "date": day["date"].isoformat(), "weekday": WEEKDAY[day["date"].weekday()],
            "status": day["status"], "max_minutes": day["max_minutes"],
            "sessions": sessions, "rest": not day["off"] and not sessions,
        })
    return {
        "generated": datetime.now(TZ).isoformat(timespec="minutes"),
        "focus": get_focus()["mode"],
        "readiness": level,
        "running_limit": run_limit,
        "done_today": [f"{TITLES[d['kind']]} ({d['name']})" for d in done_today],
        "weeks": [{"monday": w["monday"].isoformat(), "phase": w.get("phase"), "deload": w.get("deload"),
                   "mix": w.get("mix")} for w in weeks],
        "days": out_days,
        "dropped": [{"week": m.isoformat(), "session": TITLES[k]} for m, k in dropped],
    }


def week_ahead(today: date | None = None) -> dict:
    """build(), cached for a minute (the dashboard, chat context and briefing all read it)."""
    return cached("week_ahead", 60, lambda: build(today))


def summary(plan: dict | None = None) -> str:
    """Plain text for LLM prompts and Telegram."""
    plan = plan or week_ahead()
    lines = []
    for d in plan["days"]:
        head = f"{d['weekday']} {d['date']}"
        if d["status"] in availability.NO_TRAINING:
            lines.append(f"{head}: no training ({d['status']})")
        elif d["rest"]:
            lines.append(f"{head}: rest")
        else:
            limit = f" [max {d['max_minutes']} min]" if d["max_minutes"] else ""
            parts = [f"{s['title']}{' (pinned)' if s['pinned'] else ''}: {s['detail']}"
                     + (f" - {s['note']}" if s["note"] else "") for s in d["sessions"]]
            lines.append(f"{head}{limit}: " + " + ".join(parts))
    if plan["dropped"]:
        lines.append("Dropped (no room this week): " + ", ".join(x["session"] for x in plan["dropped"]))
    return "\n".join(lines)


EMOJI = {"ride": "🚴", "run": "🏃", "gym": "🏋️"}


def markdown(plan: dict | None = None) -> str:
    """The next 7 days for Telegram."""
    plan = plan or week_ahead()
    lines = ["**Next 7 days**" + (f" (readiness {plan['readiness']})" if plan["readiness"] != "green" else "")]
    for d in plan["days"]:
        head = f"**{d['weekday']} {d['date'][8:]}.{d['date'][5:7]}.**"
        if d["status"] in availability.NO_TRAINING:
            lines.append(f"{head} - no training ({d['status']})")
            continue
        if d["rest"]:
            lines.append(f"{head} - rest")
            continue
        limit = f" (max {d['max_minutes']} min)" if d["max_minutes"] else ""
        lines.append(f"{head}{limit}")
        for s in d["sessions"]:
            tag = " 📌" if s["pinned"] else " 🔥" if s["hard"] and s["sport"] != "gym" else ""
            lines.append(f"{EMOJI[s['sport']]} {s['title']}{tag}: {s['detail']}"
                         + (f" _({s['note']})_" if s["note"] else ""))
    if plan["dropped"]:
        lines.append("_Dropped for lack of days: " + ", ".join(x["session"] for x in plan["dropped"]) + "._")
    lines.append("_It re-plans itself after every sync: a missed session moves or the filler drops._")
    return "\n".join(lines)
