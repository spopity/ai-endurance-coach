"""Parsing gym logs and the progression rule (core/gym_log.py)."""
from core import gym_log as G


def test_parse_the_documented_format():
    items, bad = G.parse("Back squat 80x5x3, RDL 60kg x 8 x 3; pull-ups bwx8x3\ncalf raises 40x12")
    assert bad == []
    assert items == [
        {"exercise": "squat", "weight_kg": 80.0, "reps": 5, "sets": 3},
        {"exercise": "romanian deadlift", "weight_kg": 60.0, "reps": 8, "sets": 3},
        {"exercise": "pull-up", "weight_kg": None, "reps": 8, "sets": 3},
        {"exercise": "calf raise", "weight_kg": 40.0, "reps": 12, "sets": 1},
    ]


def test_unreadable_parts_are_reported():
    items, bad = G.parse("squat 80x5x3, felt great")
    assert len(items) == 1 and bad == ["felt great"]


def test_decimal_comma():
    assert G.parse("bench 52,5x6x3")[0][0]["weight_kg"] == 52.5


def test_progression():
    squat = {"exercise": "squat", "weight_kg": 80, "reps": 5, "sets": 3}
    bench = {"exercise": "bench press", "weight_kg": 50, "reps": 6, "sets": 3}
    assert G.next_step(squat, 7) == "squat 82.5 kg 3x5"
    assert G.next_step(squat, None) == "squat 82.5 kg 3x5"
    assert G.next_step(squat, 9).startswith("squat 80 kg 3x5 (repeat")
    assert G.next_step(bench, 8) == "bench press 51 kg 3x6"
    assert G.next_step({"exercise": "pull-up", "weight_kg": None, "reps": 8, "sets": 3}, 7) == "pull-up bodyweight 3x9"
