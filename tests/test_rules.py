"""Readiness, plan targets and injury rules: the thresholds the athlete agreed on (2026-11-22)."""
from datetime import date, timedelta

import pytest

from core import periodization as P
from core.injury_checks import check_run_spikes
from core.readiness import running_limit, verdict


def f(area: str, severity: str, title: str = "x") -> dict:
    return {"area": area, "severity": severity, "title": title}


# --- readiness ---

def test_all_clear_is_green():
    assert verdict(0, {"status": "normal"}, [])[0] == "green"


def test_one_recovery_signal_trims_not_stops():
    level, text = verdict(-5, {"status": "below"}, [])
    assert level == "amber" and "trim it ~20%" in text


def test_two_recovery_signals_together_are_red():
    assert verdict(-5, {"status": "below"}, [], sleep={"signal": "2 poor nights in a row"})[0] == "red"


def test_hard_stops_are_red():
    assert verdict(-31, None)[0] == "red"
    assert verdict(0, {"status": "low"})[0] == "red"
    assert verdict(0, None, [f("Load", "high")])[0] == "red"
    assert verdict(0, None, subjective=(["ill"], []))[0] == "red"


def test_heavy_but_not_deep_fatigue_is_amber():
    assert verdict(-25, None)[0] == "amber"


def test_running_findings_only_limit_running():
    findings = [f("Running", "high", "Longest run +120 pct")]
    assert verdict(0, None, findings)[0] == "green"
    assert running_limit(findings)["limit"] == "easy_short"
    assert running_limit([f("Running", "caution")])["limit"] == "no_hard_run"
    assert running_limit([])["limit"] == "none"


def test_load_caution_is_amber_not_red():
    assert verdict(0, None, [f("Load", "caution"), f("Load", "caution", "y")])[0] == "amber"


# --- plan targets ---

def test_normal_build_week_ramps_by_three():
    lo, hi = P.week_targets("build_2", 40)
    center = 7 * 40 + 7 * 3 / P.CTL_GAIN_PER_WEEK
    assert lo == round(center * 0.9) and hi == round(center * 1.1)


def test_comeback_ramp_is_faster_but_capped():
    normal = P.week_targets("build_2", 30)
    fast = P.week_targets("build_2", 30, back=True)
    assert fast[0] > normal[0]
    capped = P.week_targets("build_2", 30, back=True, cap=fast[0] * 0.95)
    assert normal[0] <= capped[0] < fast[0]
    # a cap below the normal ramp never pulls the week under it
    assert P.week_targets("build_2", 30, back=True, cap=1) == normal


def test_comeback_only_from_the_agreed_date_and_below_ctl_50():
    assert P.comeback(30, P.COMEBACK_FROM)
    assert not P.comeback(30, P.COMEBACK_FROM - timedelta(days=7))
    assert not P.comeback(50, P.COMEBACK_FROM)


def test_unavailable_days_scale_the_week():
    full, five = P.week_targets("build_2", 40), P.week_targets("build_2", 40, available_days=5)
    assert five[0] == pytest.approx(full[0] * 5 / 7, abs=1)


def test_recovery_week_is_lighter():
    assert P.week_targets("recovery", 40)[1] < 7 * 40


# --- injury: single-run spikes ---

def run(day: date, km: float) -> dict:
    return {"day": day, "distance_m": km * 1000, "name": "Run", "type": "Run", "start_time": day}


TODAY = date(2026, 11, 22)


def spikes(runs):
    out = []
    check_run_spikes(runs, TODAY, out)
    return out


def test_short_runs_after_a_break_are_not_a_spike():
    """The 8 km floor: 7 km after 4 km runs is not +75%."""
    assert spikes([run(TODAY - timedelta(days=10), 4), run(TODAY - timedelta(days=1), 7)]) == []


def test_real_spike_is_flagged():
    out = spikes([run(TODAY - timedelta(days=10), 10), run(TODAY - timedelta(days=1), 14)])
    assert out and out[0]["severity"] == "caution"
    out = spikes([run(TODAY - timedelta(days=10), 10), run(TODAY - timedelta(days=1), 21)])
    assert out and out[0]["severity"] == "high"
