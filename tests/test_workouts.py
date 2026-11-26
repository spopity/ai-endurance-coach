"""Workout text for Intervals.icu (core/workouts.py) and the checks on converted pinned sessions."""
import re

import pytest

from core import workouts as W

DAY = {"max_minutes": None}
WEEK = {"kind": "build_2", "deload": False}


def session(kind: str, detail: str, sport: str = "ride", hard: bool = True, pinned: bool = False) -> dict:
    return {"kind": kind, "sport": sport, "title": kind, "detail": detail, "hard": hard, "pinned": pinned, "note": None}


BIKE_DETAILS = [
    "VO2max 5x4 min at 297-336 W, equal easy recoveries; 15 min warm-up, 10 min cool-down (~65 min)",
    "Tempo 2x15 min at 213-244 W, 5 min easy between, inside a 75 min Zone 2 ride",
    "Sweet spot 3x15 min at 246-263 W (or 2x20), 5 min easy between (~90 min)",
    "Threshold 3x10 min at 266-294 W, 5 min easy between (~75 min)",
    "Sweet spot 3x12 min at 246-263 W, 5 min easy between (~75 min)",
]
RUN_DETAILS = [
    ("run_q", "Tempo 2x12 min at 150-157 bpm, 3 min jog between; 10 min easy warm-up with strides, 8 min cool-down"),
    ("run_q", "Hills 8x60 s hard uphill, jog down; 10 min easy warm-up with strides, 8 min cool-down"),
    ("run_q", "Threshold 5x6 min sub-threshold at 157-162 bpm (never above 162), 1 min jog between; "
              "10 min easy warm-up with strides"),
    ("long_run", "60-75 min easy, HR under 149, conversational"),
    ("run", "35-45 min easy, HR under 149, finish with 6 x 20 s strides"),
]


@pytest.mark.parametrize("detail", BIKE_DETAILS)
def test_bike_workouts_parse_cleanly(detail):
    w = W.build_workout(session("bike_q", detail), DAY, WEEK, "green")
    total, _, problems = W.parse_check(w["description"])
    assert problems == [] and w["structured"] and w["type"] == "Ride"
    assert total == w["moving_time_sec"]


@pytest.mark.parametrize("kind,detail", RUN_DETAILS)
def test_run_workouts_use_hr_zones(kind, detail):
    w = W.build_workout(session(kind, detail, "run", hard=kind == "run_q"), DAY, WEEK, "green")
    total, _, problems = W.parse_check(w["description"])
    # Heart rate only: HR zones, or % of LTHR for sub-threshold reps - never % of FTP.
    assert problems == [] and "HR" in w["description"] and "%" not in re.sub(r"\d+-\d+% LTHR", "", w["description"])
    assert w["type"] == "Run"


def test_sub_threshold_run_holds_just_under_lthr():
    w = W.build_workout(session("run_q", RUN_DETAILS[2][1], "run"), DAY, WEEK, "green")
    assert "Main set 5x" in w["description"] and "6m 94-97% LTHR" in w["description"]
    assert "- Jog 1m Z1 HR" in w["description"]


def test_ftp_test_warms_up_with_lt1_steps():
    w = W.build_workout(session("ftp_test", "test"), DAY, WEEK, "green")
    steps = re.findall(r"- .*?4m (\d+)% 90rpm", w["description"])
    assert steps == ["55", "65", "72", "80"]


def test_cs_test_has_a_3_and_a_9_minute_effort():
    w = W.build_workout(session("run_test", "test", "run"), DAY, WEEK, "green")
    assert "3m Z5 HR" in w["description"] and "9m Z5 HR" in w["description"]
    assert "20m Z1 HR" in w["description"]   # full recovery between


def test_zone_2_follows_lt1(monkeypatch):
    monkeypatch.setattr(W, "_z2_hi", lambda: 78)
    w = W.build_workout(session("long_ride", "120-135 min in Zone 2, steady", hard=False), DAY, WEEK, "green")
    assert "60-78% 85-95rpm" in w["description"]


def test_amber_drops_a_rep_and_adds_the_warm_up_test():
    s = session("bike_q", BIKE_DETAILS[4])
    green = W.parse_check(W.build_workout(s, DAY, WEEK, "green")["description"])[1]
    amber_w = W.build_workout(s, DAY, WEEK, "amber")
    amber = W.parse_check(amber_w["description"])[1]
    assert max(r for r, _ in green) == 3 and max(r for r, _ in amber) == 2
    assert W.WARMUP_TEST in amber_w["description"]


def test_short_day_fits_the_time():
    s = session("bike_q", BIKE_DETAILS[3])
    w = W.build_workout(s, {"max_minutes": 45}, WEEK, "green")
    assert w["moving_time_sec"] <= 45 * 60
    reps = max(r for r, _ in W.parse_check(w["description"])[1])
    assert reps >= 1


def test_gym_is_text_only():
    w = W.build_workout(session("gym_legs", "Squats 4x6", "gym"), DAY, WEEK, "green")
    assert w["type"] == "WeightTraining" and not w["structured"]


# --- the pinned-session checker ---

PIN = ("Threshold run 42-45 min by HR: 10 min easy warm-up + 3x20 s strides, 3x7 min at 150-155 bpm "
       "with 2 min easy jog recoveries, 6-8 min easy cool-down")
GOOD = """Warmup
- 10m Z2 HR

Strides 3x
- Stride 20s Z5 HR
- Walk/jog 40s Z1 HR

Main set 3x
- Threshold 7m Z4 HR
- Jog 2m Z1 HR

Cooldown
- 7m Z1 HR"""


def test_faithful_conversion_passes():
    total, problems = W.check_pinned(PIN, GOOD)
    assert problems == [] and total == 47 * 60


@pytest.mark.parametrize("broken,why", [
    (GOOD.replace("7m Z4 HR", "7m 160-167bpm"), "no target"),
    (GOOD.replace("Main set 3x", "Main set 2x"), "3x7"),
    (GOOD.replace("- 7m Z1 HR", "- 40m Z1 HR"), "doesn't fit"),
    (GOOD.replace("Strides 3x", "Strides 3x20s"), "stray"),
    ("Just ride", "no steps"),
])
def test_unfaithful_conversions_are_rejected(broken, why):
    _, problems = W.check_pinned(PIN, broken)
    assert any(why in p for p in problems), problems


@pytest.mark.parametrize("text,seconds", [(" 10m 50%", 600), (" 1m30 110%", 90), (" 30s Z5 HR", 30),
                                          (" 1h 60%", 3600), (" Cue 90s 120%", 90)])
def test_step_durations(text, seconds):
    assert W._step_seconds(text) == seconds


@pytest.mark.parametrize("kind,sport", [("ftp_test", "ride"), ("run_test", "run")])
def test_test_workouts_parse(kind, sport):
    w = W.build_workout(session(kind, "test", sport), DAY, WEEK, "green")
    total, _, problems = W.parse_check(w["description"])
    assert problems == [] and 55 * 60 <= total <= 75 * 60


def test_long_run_follows_the_plan_minutes():
    w = W.build_workout(session("long_run", "75-80 min easy, HR under 149, conversational", "run", hard=False),
                        DAY, WEEK, "green")
    assert w["moving_time_sec"] == 77 * 60


def test_ride_percentages_follow_the_training_ftp(monkeypatch):
    # FTP tested over 8 weeks ago: the plan trains on eFTP 266 while Intervals still has 285.
    monkeypatch.setattr(W, "_ftp_scale", lambda: 266 / 285)
    w = W.build_workout(session("bike_q", BIKE_DETAILS[4]), DAY, WEEK, "green")
    assert "82-88%" in w["description"] and "88-94%" not in w["description"]
    assert W.scale_percent("Ramp 50-75% 90rpm", 1.0) == "Ramp 50-75% 90rpm"


@pytest.mark.parametrize("detail", [
    "150-165 min in Zone 2 (168-202 W), steady, fuel 60-80 g carbs/h; in the last 40 min: 2x10 min sweet spot "
    "(246-263 W), 5 min easy between - fatigue resistance (durability)",
    "Durability test (~150 min): Zone 2 (168-202 W) until 1050 kJ (15 kJ/kg, about 115 min), 5 min easy, then 20 min "
    "all-out, evenly paced; 10 min easy cool-down. Fuel 80 g carbs/h from the start. Name the activity 'Durability test'",
])
def test_long_ride_variants_parse(detail):
    w = W.build_workout(session("long_ride", detail, hard=False), DAY, WEEK, "green")
    total, _, problems = W.parse_check(w["description"])
    assert problems == [] and total == w["moving_time_sec"]


def test_workout_starts_with_fuel_line():
    w = W.build_workout(session("bike_q", BIKE_DETAILS[4]), DAY, WEEK, "green")
    first = w["description"].split("\n\n")[0]
    assert first.splitlines()[-1].startswith("Fuel: ~105 g carbs 2-3 h before")


# --- amber on easy sessions, change detection, push timing ---

def test_amber_trims_an_easy_long_run_by_a_fifth():
    s = session("long_run", "75-80 min easy, HR under 149, conversational", "run", hard=False)
    w = W.build_workout(s, DAY, WEEK, "amber")
    assert w["moving_time_sec"] == 60 * 60 and w["name"].startswith("Long run 60 min")
    assert W.WARMUP_TEST in w["description"]
    assert W.build_workout(s, DAY, WEEK, "green")["moving_time_sec"] == 77 * 60


def test_a_shorter_workout_counts_as_changed_but_a_new_fuel_line_doesnt():
    s = session("long_run", "75-80 min easy, HR under 149, conversational", "run", hard=False)
    green, amber = W.build_workout(s, DAY, WEEK, "green"), W.build_workout(s, DAY, WEEK, "amber")
    assert W._signature(green) != W._signature(amber)
    refuel = dict(green, description="Fuel: other numbers\n\n" + W._steps(green["description"]))
    assert W._signature(green) == W._signature(refuel)


@pytest.fixture
def one_day(tmp_path, monkeypatch):
    """A plan with one long run today; Intervals.icu calls recorded instead of sent."""
    from datetime import datetime
    today = datetime.now(W.TZ).date()
    monkeypatch.setattr(W, "STATE_DIR", str(tmp_path))
    level = {"readiness": "green"}
    day = {"date": today.isoformat(), "weekday": "Thu", "status": "available", "max_minutes": None,
           "sessions": [session("long_run", "75-80 min easy, HR under 149, conversational", "run", hard=False)]}
    monkeypatch.setattr(W, "_plan_day", lambda d: (day, {"weeks": [], "readiness": level["readiness"]}))
    monkeypatch.setattr(W, "_week_kind", lambda monday: ("build_2", None))
    monkeypatch.setattr(W, "_weight", lambda: 70)
    calls = []

    class Resp:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return {"id": 42, **self.body}
    monkeypatch.setattr(W, "_api", lambda method, path, **kw: calls.append((method, path, kw.get("json"))) or Resp(kw.get("json") or {}))
    return today, level, calls


def test_push_before_the_morning_check_is_queued_then_sent(one_day):
    today, level, calls = one_day
    assert W.push(today.isoformat(), "long_run")["status"] == "queued" and calls == []
    level["readiness"] = "amber"                     # the morning check: amber
    sent = W.morning_sync(today)
    assert len(calls) == 1 and calls[0][0] == "POST" and calls[0][2]["moving_time"] == 60 * 60
    assert "pushed" in sent[0] and W.for_day(today)["workouts"][0]["status"] == "pushed"


def test_morning_check_updates_a_workout_pushed_the_day_before(one_day):
    today, level, calls = one_day
    W.morning_sync(today)                            # morning done, green: the push goes straight up
    W.push(today.isoformat(), "long_run")
    assert calls[-1][0] == "POST" and calls[-1][2]["moving_time"] == 77 * 60
    level["readiness"] = "amber"                     # readiness turns amber: the same event is updated
    assert W.for_day(today)["workouts"][0]["changed"]
    sent = W.morning_sync(today)
    assert calls[-1][:2] == ("PUT", "/events/42") and calls[-1][2]["moving_time"] == 60 * 60
    assert "updated" in sent[0] and not W.for_day(today)["workouts"][0]["changed"]
