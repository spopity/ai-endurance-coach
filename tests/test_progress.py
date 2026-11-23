"""Progression levels (core/progression.py) and the progress check (core/progress.py): the rules
that decide session size and when the stimulus changes. Pure functions only - no database or API."""
from datetime import date, timedelta

from core import progress as G
from core import progression as P
from core import schedule

D0 = date(2026, 9, 1)


def ride(day_offset: int, summary: list[str], ftp: float = 280, rpe: int | None = None, planned=None, share=None):
    s = {"day": D0 + timedelta(days=day_offset), "name": "x", "rpe": rpe, "done": P.ride_work(summary, ftp)}
    if planned:
        s["planned"], s["plan_share"] = planned, share
    return s


# --- what a session did ---

def test_interval_summary_is_parsed_into_bands():
    work = P.ride_work(["3x 9m54s 254w", "1x 15s 600w", "5x 3m 320w"], 280)
    assert work == {"sweet_spot": 3 * 594, "vo2": 5 * 180}   # the sprint is too short for any type


def test_hr_only_lines_and_no_ftp_give_nothing():
    assert P.ride_work(["10x 6m10s 133bpm"], 280) == {}
    assert P.ride_work(["3x 10m 254w"], None) == {}


def test_an_easy_run_drifting_into_z3_is_not_a_tempo_run():
    drift = [1068, 1633, 1031, 28, 0, 0, 0]          # 27% of the run in Z3+
    tempo = [600, 600, 1500, 100, 0, 0, 0]           # 57% in Z3+
    assert "run_tempo" not in P.run_work(drift, "Lunch run")
    assert P.run_work(tempo, "Tempo")["run_tempo"] == round(1600 / P.RUN_ZONE_CREDIT)


# --- the level rules ---

def test_completed_sessions_climb_the_ladder():
    start = P.START_LEVEL["sweet_spot"]
    sessions = [ride(i * 7, ["3x 12m 255w"]) for i in range(2)]
    lv = P.replay(sessions, D0 + timedelta(days=10))["levels"]["sweet_spot"]["level"]
    assert lv == start + 1   # 3x12 at level 1 = up; the second 3x12 is short of level 2 (2x20) -> repeat


def test_a_very_hard_rpe_holds_the_level():
    start = P.START_LEVEL["sweet_spot"]
    lv = P.replay([ride(0, ["3x 12m 255w"], rpe=9)], D0 + timedelta(days=1))["levels"]["sweet_spot"]["level"]
    assert lv == start


def test_a_big_day_jumps_two_levels():
    start = P.START_LEVEL["sweet_spot"]
    lv = P.replay([ride(0, ["3x 20m 255w"])], D0 + timedelta(days=1))["levels"]["sweet_spot"]["level"]
    assert lv == start + 2


def test_unplanned_partials_never_step_down_but_planned_ones_do():
    start = P.START_LEVEL["sweet_spot"]
    unplanned = [ride(i * 3, ["2x 12m 255w"]) for i in range(3)]
    assert P.replay(unplanned, D0 + timedelta(days=10))["levels"]["sweet_spot"]["level"] == start
    dose = ("sweet_spot", P.work("sweet_spot", start))
    planned = [ride(i * 3, [], planned=dose, share=0.6) for i in range(2)]
    assert P.replay(planned, D0 + timedelta(days=7))["levels"]["sweet_spot"]["level"] == start - 1


def test_time_off_costs_a_level_per_four_weeks():
    sessions = [ride(0, ["3x 12m 255w"]), ride(3, ["2x 20m 255w"])]
    lv = P.replay(sessions, D0 + timedelta(days=3))["levels"]["sweet_spot"]["level"]
    later = P.replay(sessions, D0 + timedelta(days=3 + 2 * P.DETRAIN_DAYS))["levels"]["sweet_spot"]["level"]
    assert later == lv - 2


def test_an_easy_rpe_session_is_not_a_quality_dose():
    lv = P.replay([ride(0, ["3x 12m 255w"], rpe=2)], D0 + timedelta(days=1))["levels"]["sweet_spot"]
    assert lv["last_done"] is None


def test_planned_workout_names_give_the_dose():
    assert P.planned_dose("Sweet spot 3x15 min (AI coach)") == ("sweet_spot", 2700)
    assert P.planned_dose("Threshold run 3x8 min (AI coach)") == ("run_threshold", 1440)
    assert P.planned_dose("Hill repeats 8x60 s (AI coach)") == ("run_hills", 480)


# --- the plan uses the levels ---

def test_the_plan_text_follows_the_level():
    t = {"ftp": 280.0, "hr": [141, 149, 157, 166], "levels": {**P.START_LEVEL, "sweet_spot": 3}}
    info = {"kind": "build_1", "phase": "Build", "deload": False}
    text = schedule._describe("bike_q", info, t, {"max_minutes": None}, False, False, "none")
    assert text.startswith("Sweet spot 3x15 min")


def test_vo2_stimulus_turns_one_bike_quality_session_into_vo2():
    t = {"ftp": 280.0, "hr": [141, 149, 157, 166], "emphasis": {"bike": "vo2"}}
    info = {"kind": "build_1", "phase": "Build", "deload": False}
    assert schedule.bike_type(info, t) == "vo2"
    assert schedule.bike_type(info, t) == "sweet_spot"   # only one a week
    assert schedule.bike_type({"kind": "base_late", "phase": "Base"}, t) == "tempo"   # never in Base


def test_long_ride_grows_from_the_longest_ride():
    assert schedule.long_ride_minutes(None, False) == "120-135 min"
    assert schedule.long_ride_minutes(150, False) == "165-180 min"
    assert schedule.long_ride_minutes(150, False, volume=True) == "180-195 min"
    assert schedule.long_ride_minutes(150, True) == "75-90 min"


# --- the progress check ---

TESTED = [{"vo2", "threshold"}] * 4


def test_trend_up_flat_down_and_untested():
    assert G.trend("p20", [250, 240, 240], TESTED) == "up"
    assert G.trend("p20", [241, 240, 240], TESTED) == "flat"
    assert G.trend("p20", [230, 240, 240], TESTED) == "down"
    assert G.trend("p20", [230, 240, 240], [set()] * 3) == "untested"   # no hard effort: not slower
    assert G.trend("p20", [244, 242, 236], TESTED) == "up"               # a slow climb over 8 weeks


def test_verdict_weighs_the_markers():
    assert G.verdict({"a": "up", "b": "flat"}) == "improving"
    assert G.verdict({"a": "up", "b": "down", "c": "down"}) == "declining"
    assert G.verdict({"a": "up", "b": "down"}) == "mixed"
    assert G.verdict({"a": "untested", "b": None}) == "no_data"


CONSISTENT = {"completed": 3, "of": 3, "consistent": True}
TODAY = date(2026, 12, 20)


def _diag(**kw):
    args = dict(sport="ride", now="flat", before="flat", consistency=CONSISTENT, readiness="green", phase="Build",
                trends={"p20": "flat", "p5": "flat"}, history=[], today=TODAY)
    args.update(kw)
    return G.diagnose(**args)


def test_a_stall_with_consistent_training_changes_the_stimulus():
    d = _diag()
    assert d["diagnosis"] == "stalled" and d["stimulus"] == "threshold" and d["stalled_weeks"] == 8


def test_no_stimulus_change_when_the_load_wasnt_done_or_after_illness_or_red():
    assert _diag(consistency={"completed": 1, "of": 3, "consistent": False})["diagnosis"] == "inconsistent"
    assert _diag(illness_end=TODAY - timedelta(days=10))["diagnosis"] == "after_illness"
    assert _diag(readiness="red")["diagnosis"] == "fatigued"
    assert _diag(now="improving")["stimulus"] is None


def test_base_only_adds_volume_and_recent_stimuli_rotate():
    assert _diag(phase="Base")["stimulus"] == "volume"
    tried = [{"sport": "ride", "stimulus": "threshold", "started": (TODAY - timedelta(days=30)).isoformat()}]
    assert _diag(history=tried)["stimulus"] == "vo2"
    old = [{"sport": "ride", "stimulus": "threshold", "started": (TODAY - timedelta(days=200)).isoformat()}]
    assert _diag(history=old)["stimulus"] == "threshold"


def test_vo2_first_when_only_the_top_end_stalls():
    assert _diag(trends={"p20": "up", "p5": "flat"})["stimulus"] == "vo2"


# --- durability (#3) and the aerobic Build gate (#4) ---

from core import durability as DU  # noqa: E402
from core import metrics as M  # noqa: E402
from core import periodization as PZ  # noqa: E402


def test_best_power_after_the_kj_threshold():
    # 70 kg x 15 kJ/kg = 1050 kJ: 5250 s at 200 W, then 20 min at 250 W.
    watts = [200] * 5250 + [250] * 1200 + [100] * 300
    late = DU.best_after(watts, 70)
    assert late["p20"] == 250 and late["after_kj"] == 1050
    assert DU.best_after([200] * 3000, 70) is None


def test_late_power_counts_only_real_efforts():
    assert DU.late_share({"p20": 240}, 260) == round(240 / 260, 3)
    assert DU.late_share({"p20": 180}, 260) is None   # ridden easy to the end


def test_only_steady_long_rides_judge_the_drift():
    rides = [{"day": D0, "moving_time_sec": 7200, "variability_index": 1.05, "intensity_factor": 0.65, "decoupling_pct": 3.2},
             {"day": D0, "moving_time_sec": 7200, "variability_index": 1.25, "intensity_factor": 0.65, "decoupling_pct": 12},
             {"day": D0, "moving_time_sec": 3600, "variability_index": 1.02, "intensity_factor": 0.65, "decoupling_pct": 1}]
    assert DU.steady_decoupling(rides) == [(D0, 3.2)]


def test_aerobic_ready_needs_two_recent_steady_rides_under_5():
    on = D0 + timedelta(days=20)
    assert DU.aerobic_ready([(D0 + timedelta(days=5), 4.1), (D0 + timedelta(days=12), 3.0)], on)
    assert not DU.aerobic_ready([(D0 + timedelta(days=12), 3.0)], on)
    assert not DU.aerobic_ready([(D0 + timedelta(days=5), 6.5), (D0 + timedelta(days=12), 3.0)], on)


def test_drift_trend_is_better_when_lower():
    assert G.trend("decoupling", [3.0, 5.0], TESTED) == "up"
    assert G.trend("decoupling", [6.5, 5.0], TESTED) == "down"


def test_build_unlocks_early_once_the_aerobic_base_is_measured():
    s = PZ.State(base_weeks=["completed"] * 3)
    why = PZ.advance(s, "base_late", "completed", 300, 330, 250, 40, aerobic_ok=True)
    assert s.phase == "Build" and "early" in why
    s2 = PZ.State(base_weeks=["completed"])   # + this week = 2 completed
    PZ.advance(s2, "base_early", "completed", 300, 330, 250, 40, aerobic_ok=True)
    assert s2.phase == "Base"   # never before BASE_EARLY_WEEKS


def test_ftp_rule_tested_for_8_weeks_then_eftp():
    test_day = date(2026, 10, 6)
    assert M.ftp_on(300, test_day, test_day + timedelta(days=56), 285) == (300, "set")
    assert M.ftp_on(300, test_day, test_day + timedelta(days=57), 285) == (285, "eftp")
    rides = [{"day": test_day, "ftp_at_time": 300, "eftp": 295, "name": "FTP test"},
             {"day": test_day + timedelta(days=30), "ftp_at_time": 300, "eftp": 292, "name": "x"},
             {"day": test_day + timedelta(days=60), "ftp_at_time": 300, "eftp": 285, "name": "x"},
             {"day": test_day + timedelta(days=65), "ftp_at_time": 300, "eftp": 290, "name": "FTP test"}]
    assert M.ftp_history(rides) == [300, 300, 285, 300]


def test_long_ride_ends_with_efforts_in_build_and_tests_every_6_weeks(week):
    plan = week(last_durability_test=date(2026, 9, 1))
    lr = [s for d in plan["days"] for s in d["sessions"] if s["kind"] == "long_ride"]
    assert lr and lr[0]["detail"].startswith("Durability test")
    plan = week()
    lr = [s for d in plan["days"] for s in d["sessions"] if s["kind"] == "long_ride"]
    assert "2x10 min sweet spot" in lr[0]["detail"]
    plan = week(phase="Base", kind="base_late", mix={"endurance": 3, "sst": 1, "vo2": 0, "run": 3, "weights": 2})
    lr = [s for d in plan["days"] for s in d["sessions"] if s["kind"] == "long_ride"]
    assert "sweet spot" not in lr[0]["detail"] and "Durability" not in lr[0]["detail"]
