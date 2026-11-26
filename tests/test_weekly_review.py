"""The weekly review (core/weekly_review.py) and the dated comeback ramp it can change."""
from datetime import date, timedelta

import pytest

from core import periodization as P
from core import weekly_review as WR

SUNDAY = date(2026, 11, 29)


@pytest.fixture
def review_env(monkeypatch, tmp_path):
    monkeypatch.setattr(WR, "REVIEW_DIR", str(tmp_path / "reviews"))
    monkeypatch.setattr(WR, "facts", lambda today: {"last_week": {"from": "2026-11-23", "to": "2026-11-29"}})
    from core import availability
    monkeypatch.setattr(availability, "day_status", lambda d, data=None: None)
    applied = []
    monkeypatch.setattr(WR, "_set_guidance", lambda text, today: applied.append(("guidance", text)))
    monkeypatch.setattr(P, "set_comeback_ramp", lambda ramp, wk: applied.append(("ramp", ramp, wk)))
    from core import adjustments
    monkeypatch.setattr(adjustments, "log", lambda *a, **k: applied.append(("log", a[0], a[1])))
    from core import next_session
    monkeypatch.setattr(next_session, "replan", lambda: None)
    return applied


def _answer(monkeypatch, proposals, summary="Solid week."):
    monkeypatch.setattr(WR, "ask", lambda fx, today: {"summary": summary, "proposals": proposals})


def test_only_valid_proposals_from_the_menu_are_offered(monkeypatch, review_env):
    _answer(monkeypatch, [
        {"action": "guidance", "params": {"text": "Long rides strictly Zone 2: drift still 7%."}, "title": "Z2", "why": "drift 7%"},
        {"action": "comeback_ramp", "params": {"ramp": 5}, "title": "keep 5", "why": "already 5"},  # no change
        {"action": "rewrite_plan", "params": {}, "title": "x", "why": "y"},                         # not on the menu
        {"action": "comeback_ramp", "params": {"ramp": 4}, "title": "ramp 4", "why": "HRV 6% under"},
        {"action": "pin", "params": {"date": "2026-12-20", "session": "x"}, "title": "far", "why": "too far"},
    ])
    r = WR.run(SUNDAY)
    assert [p["action"] for p in r["proposals"]] == ["guidance", "comeback_ramp"]
    assert [p["id"] for p in r["proposals"]] == [0, 1] and all(p["status"] == "pending" for p in r["proposals"])
    assert WR.load("2026-11-29") == r


def test_apply_and_discard_are_once_each_and_logged(monkeypatch, review_env):
    _answer(monkeypatch, [
        {"action": "guidance", "params": {"text": "Keep the long run easy."}, "title": "Easy long run", "why": "legs heavy"},
        {"action": "comeback_ramp", "params": {"ramp": 4}, "title": "Ramp 4", "why": "HRV 6% under"},
    ])
    WR.run(SUNDAY)
    assert WR.resolve("2026-11-29", 0, True, SUNDAY).startswith("Applied")
    assert WR.resolve("2026-11-29", 0, True, SUNDAY) == "Already applied."
    assert WR.resolve("2026-11-29", 1, False, SUNDAY).startswith("Discarded")
    assert ("guidance", "Keep the long run easy.") in review_env
    assert ("log", "review", "Easy long run") in review_env
    assert not any(x[0] == "ramp" for x in review_env)
    assert [p["status"] for p in WR.load("2026-11-29")["proposals"]] == ["applied", "discarded"]


def test_ramp_applies_from_next_monday(monkeypatch, review_env):
    _answer(monkeypatch, [{"action": "comeback_ramp", "params": {"ramp": 6}, "title": "Ramp 6", "why": "absorbed"}])
    WR.run(SUNDAY)
    WR.resolve("2026-11-29", 0, True, SUNDAY)
    assert ("ramp", 6.0, date(2026, 11, 30)) in review_env


def test_a_new_review_expires_the_last_ones_open_proposals(monkeypatch, review_env):
    _answer(monkeypatch, [{"action": "guidance", "params": {"text": "a"}, "title": "a", "why": "a"}])
    WR.run(SUNDAY)
    _answer(monkeypatch, [], summary="Quiet week.")
    r = WR.run(date(2026, 12, 6))
    assert WR.load("2026-11-29")["proposals"][0]["status"] == "expired"
    assert "No changes proposed" in WR.markdown(r)
    assert WR.resolve("2026-11-29", 0, True, date(2026, 12, 6)) == "Already expired."


def test_comeback_ramp_is_dated_so_past_weeks_keep_their_target():
    hist = [(date(2026, 11, 23), 5.0), (date(2026, 11, 30), 4.0), (date(2026, 12, 14), 6.0)]
    assert P.comeback_ramp(date(2026, 11, 23), hist) == 5.0
    assert P.comeback_ramp(date(2026, 12, 7), hist) == 4.0
    assert P.comeback_ramp(date(2026, 12, 21), hist) == 6.0
    four = P.week_targets("build_2", 30, back=True, ramp=4.0)
    five = P.week_targets("build_2", 30, back=True, ramp=5.0)
    assert four[0] < five[0]


def test_push_sits_above_the_plans_own_ramp():
    from core.push import week_ceiling
    at = lambda ramp, level: week_ceiling(30, level, 0, 7, None, comeback=True, plan_ramp=ramp)
    assert at(5.0, 1) == week_ceiling(30, 1, 0, 7, None, comeback=True)        # the default: 6/week
    assert at(4.0, 1) < at(5.0, 1) < at(5.0, 2)


# --- form lines follow the plan's ramp; monotony needs high strain (approved 2026-11-26) ---

BUILD = {"phase": "Build", "is_deload": False, "target_weekly_load_min": 265, "target_weekly_load_max": 324}


def test_form_lines_sit_below_what_the_plans_ramp_builds():
    thu = date(2026, 11, 26)
    comeback = P.form_lines(30, thu, BUILD)                       # +5/week comeback: form ~-25 by design
    assert comeback == {"expected": -25.0, "ramp": 5.0, "amber": -35.0, "red": -40.0}
    assert P.form_lines(60, thu, BUILD)["amber"] == -25.0         # normal Build +3: -15 expected
    assert P.form_lines(60, thu, {"phase": "Base", "is_deload": False})["amber"] == -20.0   # never above -20
    assert P.form_lines(30, thu, {"phase": "Recovery", "is_deload": True})["amber"] == -20.0
    assert P.form_lines(30, thu, None) == {"expected": 0.0, "ramp": 0.0, "amber": -20.0, "red": -30.0}


def test_planned_fatigue_no_longer_trims_the_day():
    from core.readiness import verdict
    lines = P.form_lines(30, date(2026, 11, 26), BUILD)
    assert verdict(-25, None, lines=lines)[0] == "green"          # was amber at -20
    assert verdict(-36, None, lines=lines)[0] == "amber"
    assert verdict(-41, None, lines=lines)[0] == "red"
    assert verdict(-25, None)[0] == "amber"                       # without a plan: the fixed lines


def _load_findings(monkeypatch, ctl, atl, daily, block):
    from core import injury_checks as I
    monkeypatch.setattr(P, "current_block", lambda today: block)
    today = date(2026, 11, 26)
    acts = [{"day": today - timedelta(days=i), "training_load": l} for i, l in enumerate(reversed(daily))]
    data = {"wellness": [{"ctl": ctl, "atl": atl, "ramp_rate": 4.0}], "activities": acts}
    findings = []
    I.check_load(data, today, findings, [], [])
    return {f["title"].split(" ")[0] + " " + f["title"].split(" ")[1]: f["severity"] for f in findings}


def test_load_checks_follow_the_plan_and_monotony_needs_strain(monkeypatch):
    even = [40, 45, 40, 45, 40, 45, 40]                           # monotony ~11, 295 load: within 324
    f = _load_findings(monkeypatch, 30, 55, even, BUILD)          # TSB -25, ACWR 1.83 (comeback)
    assert f["Form (TSB)"] == "info" and f["Acute:chronic load"] == "ok" and f["Training monotony"] == "info"
    heavy = [55, 60, 55, 60, 55, 60, 55]                          # 400 > 324: high strain
    assert _load_findings(monkeypatch, 30, 55, heavy, BUILD)["Training monotony"] == "caution"
    f = _load_findings(monkeypatch, 30, 66, even, BUILD)          # TSB -36: beyond the plan
    assert f["Form (TSB)"] == "caution" and f["Acute:chronic load"] == "caution"
    assert _load_findings(monkeypatch, 30, 55, even, None)["Form (TSB)"] == "caution"   # no plan: fixed -20
