from datetime import date, timedelta

from core.mechanical import finding, impact, streams

TODAY = date(2026, 3, 1)


def run(day, km, gain=0, hz=None):
    return {"day": day, "type": "Run", "distance_m": km * 1000, "gain": gain, "hr_zone_times": hz}


def test_impact_by_session():
    assert impact(run(TODAY, 10)) == 10
    assert impact(run(TODAY, 10, gain=200)) == 12                       # hills
    assert round(impact(run(TODAY, 10, hz=[0, 0, 75, 25, 0, 0, 0])), 2) == 12.5   # 25% in Z4+
    assert impact({"type": "VirtualRide", "distance_m": 40000}) == 0
    assert impact({"type": "WeightTraining", "name": "Upper body", "moving_time_sec": 2700}) == 0
    assert impact({"type": "WeightTraining", "name": "Legs", "moving_time_sec": 2700, "rpe": 7}) == 10.5
    assert impact({"type": "Walk", "distance_m": 4000}) == 1


def steady_weeks(weeks, km_per_run, runs_per_week=3):
    out = []
    for w in range(weeks):
        for r in range(runs_per_week):
            out.append(run(TODAY - timedelta(days=7 * w + 2 * r), km_per_run))
    return out


def test_steady_running_is_ok():
    s = streams(steady_weeks(20, 8), TODAY, ctl=50, atl=50)
    assert 0.8 <= s["impact_ratio"] <= 1.2
    assert finding(s)[0] == "ok"


def test_run_spike_hidden_by_calm_cardio_is_flagged():
    acts = steady_weeks(20, 5, runs_per_week=2) + [run(TODAY - timedelta(days=d), 12) for d in (0, 2, 4)]
    s = streams(acts, TODAY, ctl=60, atl=62)     # cardio calm: the rides carry the TSS
    assert s["impact_ratio"] >= 1.5 and s["cardio_ratio"] < 1.1
    sev, title, detail = finding(s)
    assert sev == "caution" and "spike is in the legs" in detail


def test_no_ratio_from_a_tiny_base():
    s = streams([run(TODAY, 10)], TODAY, ctl=50, atl=50)
    assert s["impact_ratio"] is None and finding(s) is None
