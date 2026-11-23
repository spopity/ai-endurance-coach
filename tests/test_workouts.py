"""Workout text for Intervals.icu (core/workouts.py) and the checks on converted pinned sessions."""
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
    ("run_q", "Threshold 3x8 min at 158-166 bpm, 2 min jog between; 10 min easy warm-up with strides"),
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
    assert problems == [] and "HR" in w["description"] and "%" not in w["description"]
    assert w["type"] == "Run"


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
    assert problems == [] and 45 * 60 <= total <= 75 * 60


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
