"""Daily carbs, the low-energy check, the goals, the adjustment log / review and the device check."""
from datetime import date, timedelta

from core import adjustments, devices, energy, goals
from core.nutrition import daily_carbs, session_minutes

TODAY = date(2027, 1, 20)


# --- daily carbs ---

def s(sport, minutes, hard=False):
    return {"sport": sport, "minutes": minutes, "hard": hard}


def test_session_minutes_from_the_text():
    assert session_minutes("VO2max 5x2 min ... (~45 min)") == 45
    assert session_minutes("60-75 min Zone 2") == 67
    assert session_minutes("60-75 min, at most 45 min today", max_minutes=45) == 45
    assert session_minutes("Squats", "gym_legs") == 45
    assert session_minutes("no number", default=30) == 30


def test_daily_carbs_follow_the_day():
    assert daily_carbs([], 70)["g_per_kg"] == (3, 4)
    assert daily_carbs([s("gym", 45)], 70)["g_per_kg"] == (4, 5)
    assert daily_carbs([s("run", 40)], 70)["g_per_kg"] == (4, 5)
    assert daily_carbs([s("ride", 45, True)], 70)["g_per_kg"] == (5, 7)
    assert daily_carbs([s("ride", 120)], 70)["g_per_kg"] == (6, 8)
    assert daily_carbs([s("ride", 180)], 70)["g_per_kg"] == (8, 10)
    d = daily_carbs([s("ride", 120)], 70)
    assert d["grams"] == (420, 560) and d["protein"] == (110, 140)


def test_daily_carbs_fuel_up_and_low_energy():
    assert daily_carbs([], 70, next_day=[s("ride", 150)])["g_per_kg"] == (4, 5)
    assert daily_carbs([], 70, next_day=[s("gym", 45)])["g_per_kg"] == (3, 4)
    low = daily_carbs([s("run", 40)], 70, low_energy=True)
    assert low["g_per_kg"] == (4.5, 5) and "low-energy" in low["why"]


# --- low energy ---

def series(start: date, days: int, f) -> dict:
    return {start + timedelta(days=i): f(i) for i in range(days)}


def test_energy_flags_weight_down_while_load_up():
    weights = {TODAY - timedelta(days=d): w for d, w in ((50, 72.0), (40, 72.2), (30, 72.1), (10, 70.8), (3, 70.9))}
    ctl = series(TODAY - timedelta(days=40), 41, lambda i: 40 + i * 0.2)
    r = energy.check(weights, ctl, {}, TODAY)
    assert r["level"] == "watch" and r["weight_change_kg"] < -1 and r["ctl_change"] >= 3


def test_energy_ok_and_unknown():
    weights = {TODAY - timedelta(days=d): 72.0 for d in (50, 40, 10, 3)}
    ctl = series(TODAY - timedelta(days=40), 41, lambda i: 40 + i * 0.2)
    assert energy.check(weights, ctl, {}, TODAY)["level"] == "ok"
    assert energy.check({TODAY - timedelta(days=3): 72.0}, ctl, {}, TODAY)["level"] == "unknown"
    # a big drop flags even without more load
    drop = {TODAY - timedelta(days=d): w for d, w in ((50, 72.0), (40, 72.0), (10, 70.0), (3, 70.1))}
    assert energy.check(drop, {}, {}, TODAY)["level"] == "watch"


# --- goals ---

FTP_GOAL = {"target_w": 270, "above": True, "by": "2027-05-01", "start": "2026-12-01", "start_w": 250}


def test_ftp_goal_path():
    on = goals.ftp_status(FTP_GOAL, 260, date(2027, 2, 1), 255, TODAY)
    assert on["target"] == 271 and on["status"] in ("on_track", "ahead")
    behind = goals.ftp_status(FTP_GOAL, 250, date(2027, 2, 15), None, TODAY)
    assert behind["status"] == "behind" and behind["per_week"] > 0
    assert goals.ftp_status(FTP_GOAL, 272, date(2027, 3, 1), None, TODAY)["status"] == "reached"
    # a test before the goal was set is judged against the start
    assert goals.ftp_status(FTP_GOAL, 250, date(2026, 10, 1), None, TODAY)["status"] == "on_track"


def test_hm_prediction_riegel_and_cs():
    dist, secs = [1000, 3000, 5000, 10000], [240, 780, 1350, 2850]
    p = goals.hm_prediction(dist, secs, None)
    assert p["time"] == round(min(t * (goals.HM_M / d) ** 1.06 for d, t in ((3000, 780), (5000, 1350), (10000, 2850))))
    assert p["source"].startswith("best 3 km")
    fast_cs = {"source": "test", "cs": 4.2, "pace": "3:58"}
    assert goals.hm_prediction(dist, secs, fast_cs)["source"].startswith("critical speed")
    assert goals.hm_prediction([500, 1000], [100, 220], None) is None


def test_hm_status():
    g = {"pace_sec_km": 280}
    ready = goals.hm_status(g, {"time": 5800, "pace": 275, "source": "x"}, 90)
    assert ready["status"] == "ready"
    assert goals.hm_status(g, {"time": 5800, "pace": 275, "source": "x"}, 50)["status"] == "close"
    assert goals.hm_status(g, {"time": 6500, "pace": 308, "source": "x"}, 90)["status"] == "building"


def test_goal_changes_message():
    before = {"ftp": {"status": "on_track", "text": "a"}}
    after = {"ftp": {"status": "behind", "text": "FTP: behind"}}
    assert "behind" in goals.changes(before, after)
    assert goals.changes(after, after) is None


# --- adjustments ---

def test_outcome_and_absorption():
    known = {"2027-01-01": {"eftp": 250, "ctl": 40}, "2027-01-30": {"eftp": 256, "ctl": 46}}
    assert adjustments.outcome("2027-01-01", known) == {"eftp": 6, "ctl": 6}
    assert adjustments.outcome("2027-01-10", known) is None
    rows, ctl = [], 30.0
    start = date(2025, 12, 29)   # a Monday: the review groups Monday-Sunday weeks
    for i in range(300):
        week = i // 7
        ramp = 6 if week % 2 else 2      # alternating hard and easy ramp weeks
        ctl += ramp / 7
        hrv = 50 * (0.9 if (week % 2 == 0 and week > 0) else 1.0)   # the week after a 6/week ramp drops
        rows.append({"day": start + timedelta(days=i), "ctl": ctl, "hrv": hrv})
    b = {x["ramp"]: x for x in adjustments.absorption(rows)}
    assert b["5-7/week"]["hrv_pct"] < b["0-3/week"]["hrv_pct"]
    assert adjustments.absorbed_ramp(adjustments.absorption(rows)) == "0-3/week"


def test_quarterly_once_on_the_first_night(tmp_path, monkeypatch):
    monkeypatch.setattr(adjustments, "REVIEW_DIR", str(tmp_path))
    monkeypatch.setattr(adjustments, "review", lambda today=None: "review text")
    assert adjustments.quarterly(date(2027, 2, 10)) is None
    assert adjustments.quarterly(date(2027, 4, 1)) == "review text"
    assert (tmp_path / "2027-Q1.md").exists()
    assert adjustments.quarterly(date(2027, 4, 1)) is None


# --- devices ---

def night(hr=True, hrv=True, sleep=True, steps=6000):
    return {"resting_hr": 50 if hr else None, "avg_sleeping_hr": 55 if hr else None, "min_hr": 45 if hr else None,
            "hrv": 50 if hrv else None, "sleep_duration_sec": 27000 if sleep else None, "steps": steps, "garmin": True}


def test_watch_heart_rate_gap():
    rows = {TODAY - timedelta(days=i): night() for i in range(2, 40)}
    rows[TODAY] = night(hr=False)
    rows[TODAY - timedelta(days=1)] = night(hr=False)
    gaps = devices.daily_gaps(rows, TODAY)
    assert set(gaps) == {"watch:heart"} and "wrist heart rate" in gaps["watch:heart"]
    rows[TODAY] = night()
    assert devices.daily_gaps(rows, TODAY) == {}


def test_long_gap_stays_flagged_and_unsynced_night_doesnt_count():
    rows = {TODAY - timedelta(days=i): night() for i in range(15, 50)}
    rows.update({TODAY - timedelta(days=i): night(hr=False) for i in range(1, 15)})
    assert "watch:heart" in devices.daily_gaps(rows, TODAY)   # 14 nights: the normal is before the gap
    rows = {TODAY - timedelta(days=i): night() for i in range(1, 40)}
    rows[TODAY] = {"garmin": False}
    assert devices.daily_gaps(rows, TODAY) == {}


def test_steps_gap_only_on_full_days():
    rows = {TODAY - timedelta(days=i): night() for i in range(3, 40)}
    rows[TODAY - timedelta(days=1)] = night(steps=100)
    rows[TODAY - timedelta(days=2)] = night(steps=50)
    rows[TODAY] = night(steps=10)
    assert "watch:steps" in devices.daily_gaps(rows, TODAY)


def test_activity_without_hr_or_power():
    hist = [{"id": i, "day": TODAY - timedelta(days=10 + i), "type": "Ride", "trainer": False, "name": "r",
             "avg_hr": 140, "avg_power_w": 200} for i in range(6)]
    new = {"id": 99, "day": TODAY, "type": "Ride", "trainer": False, "name": "Morning Ride", "avg_hr": None,
           "avg_power_w": None}
    gaps = devices.activity_gaps([new], hist)
    assert set(gaps) == {"act:99:hr", "act:99:power"}
    # indoor rides are compared with indoor rides only
    indoor = dict(new, type="VirtualRide", avg_power_w=210)
    assert devices.activity_gaps([indoor], hist) == {}


def test_scale_gap():
    regular = [TODAY - timedelta(days=d) for d in (16, 25, 40)]
    assert "scale" in devices.scale_gap(regular, TODAY)
    assert devices.scale_gap(regular + [TODAY - timedelta(days=2)], TODAY) == {}
    assert devices.scale_gap([TODAY - timedelta(days=200)], TODAY) == {}
