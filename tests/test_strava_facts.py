from core.plan import classify
from core.strava_notes import plan_label, run_target


def test_plan_label():
    assert plan_label("Threshold run 3x7 min (AI coach)") == "Threshold run"
    assert plan_label("Long ride 125 min + 2x10 SS (AI coach)") == "Long ride"
    assert plan_label("VO2max 5x2 min (AI coach)") == "VO2max"
    assert plan_label("Zwift race") == "Zwift race"


def test_threshold_run_shows_time_in_hard_zones():
    planned = {"name": "Threshold run 3x7 min (AI coach)", "completed": True}
    assert run_target([550, 762, 388, 990, 13, 0, 0], 2710, planned) == ("Threshold run", "16m30s Z4 + 6m28s Z3 HR")


def test_unplanned_runs_by_hr():
    assert run_target([3690, 2, 0, 0, 0, 0, 0], 3693, None) == ("Long run", "100% in Z1-2 HR")
    assert run_target([1800, 300, 0, 0, 0, 0, 0], 2100, None) == ("Easy run", "100% in Z1-2 HR")
    assert run_target([1068, 1633, 1031, 28, 0, 0, 0], 3768, None)[0] == "Quality run"
    assert run_target(None, 1800, None) == ("Easy run", None)


def test_incomplete_plan_does_not_name_the_run():
    planned = {"name": "Threshold run 3x7 min (AI coach)", "completed": False}
    assert run_target([3000, 600, 0, 0, 0, 0, 0], 3600, planned)[0] == "Long run"


def test_vo2_5x2_counts_as_vo2():
    pz = [{"id": "Z4", "secs": 332}, {"id": "Z5", "secs": 407}, {"id": "Z6", "secs": 93}, {"id": "SS", "secs": 110}]
    assert classify("VirtualRide", pz, None) == "vo2"
    assert classify("VirtualRide", [{"id": "Z5", "secs": 300}], None) == "endurance"
