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


def test_morning_verdict_holds_for_the_day(monkeypatch):
    from datetime import date
    from core import readiness as R
    today = {"d": date(2026, 11, 26)}
    monkeypatch.setattr(R, "_local_today", lambda: today["d"])
    for name in ("hrv_status", "sleep_status", "illness_status", "feedback_signals"):
        monkeypatch.setattr(R, name, lambda *a, **k: None)
    monkeypatch.setattr(R, "cached", lambda key, ttl, fn: {"findings": [], "overall": "ok"})
    tsb = {"v": -21.0}
    monkeypatch.setattr(R, "current_readiness", lambda: R.readiness_context(tsb["v"]))
    monkeypatch.setattr(R, "night_data", lambda d=None: {"ready": True, "missing": [], "tsb": tsb["v"],
                                                         "rhr_watch": 50.0, "rhr_intervals": 50.0})
    assert R.freeze_morning()["level"] == "amber"
    tsb["v"] = -18.0                       # a later sync re-scores the loads: the day stays amber
    r = R.readiness_context(tsb["v"])
    assert r["level"] == "amber" and r["held_from_morning"]
    tsb["v"] = -35.0                       # worse later in the day: the worse one wins
    assert R.readiness_context(tsb["v"])["level"] == "red"
    assert R.freeze_morning()["level"] == "amber"   # a second briefing doesn't replace it
    tsb["v"] = -5.0                        # the next day starts fresh
    today["d"] = date(2026, 11, 27)
    assert R.readiness_context(tsb["v"])["level"] == "green"


def _night_rows(monkeypatch, R, row, profile_rhr):
    def q(sql, params=None):
        if "athlete_profile" in sql:
            return [{"resting_hr": profile_rhr}]
        if "FROM daily_metrics WHERE day" in sql:
            return [row] if row else []
        return [{"ctl": 60.0, "atl": 80.0}]
    monkeypatch.setattr(R, "query", q)


def test_night_data_waits_for_the_watch_and_intervals_resting_hr(monkeypatch):
    from datetime import date, datetime
    from core import readiness as R
    d = date(2026, 11, 26)
    _night_rows(monkeypatch, R, None, 55)                      # 07:00, still asleep: nothing in
    n = R.night_data(d)
    assert not n["ready"] and "last night's sleep" in n["missing"] and n["tsb"] == -20.0
    night = {"sleep_end": datetime(2026, 11, 26, 6, 14), "hrv": 53.0, "resting_hr": 50}
    _night_rows(monkeypatch, R, night, 55)                     # watch synced, Intervals still 55
    n = R.night_data(d)
    assert not n["ready"] and n["missing"] == ["Intervals.icu's updated resting HR (still 55 bpm, watch 50)"]
    _night_rows(monkeypatch, R, night, 52)                     # within 2 bpm: caught up
    assert R.night_data(d)["ready"]


def test_early_briefing_does_not_fix_the_day_but_the_cutoff_does(monkeypatch):
    from datetime import date
    from core import readiness as R
    d = date(2026, 11, 26)
    monkeypatch.setattr(R, "_local_today", lambda: d)
    monkeypatch.setattr(R, "current_readiness", lambda: {"level": "amber", "verdict": "trim"})
    missing = {"ready": False, "missing": ["last night's sleep"], "tsb": -20.0, "rhr_watch": None, "rhr_intervals": 55.0}
    monkeypatch.setattr(R, "night_data", lambda d=None: missing)
    assert R.freeze_morning(force=False) is None and R.morning_verdict() is None   # /briefing at 06:50
    m = R.freeze_morning(force=True)                                               # 10:00 cutoff
    assert m["level"] == "amber" and m["missing"] == ["last night's sleep"] and R.morning_verdict() == m


def test_midday_log_records_both_verdicts_without_the_hold(monkeypatch, tmp_path):
    import json
    from datetime import date
    from core import readiness as R
    d = date(2026, 11, 26)
    monkeypatch.setattr(R, "_local_today", lambda: d)
    monkeypatch.setattr(R, "MIDDAY_LOG", str(tmp_path / "log.jsonl"))
    for name in ("hrv_status", "sleep_status", "illness_status", "feedback_signals"):
        monkeypatch.setattr(R, name, lambda *a, **k: None)
    monkeypatch.setattr(R, "cached", lambda key, ttl, fn: {"findings": [], "overall": "ok"})
    tsb = {"v": -21.0}
    monkeypatch.setattr(R, "_tsb", lambda: tsb["v"])
    monkeypatch.setattr(R, "night_data", lambda d=None: {"ready": True, "missing": [], "tsb": tsb["v"],
                                                         "rhr_watch": 50.0, "rhr_intervals": 50.0})
    R.freeze_morning()
    tsb["v"] = -18.0                                      # re-scored by 13:00
    assert R.current_readiness()["level"] == "amber"      # the day still holds
    R.log_midday()
    e = json.loads(open(R.MIDDAY_LOG).read().splitlines()[-1])
    assert e["morning"]["level"] == "amber" and e["morning"]["tsb"] == -21.0
    assert e["midday"]["level"] == "green" and e["midday"]["tsb"] == -18.0


def test_resting_hr_set_twice_a_week_from_the_watch_median(monkeypatch, tmp_path):
    from datetime import date
    from core import resting_hr as H
    monkeypatch.setattr(H, "STATE_FILE", str(tmp_path / "rhr.json"))
    monkeypatch.setattr(H, "query", lambda sql, p=None: [{"resting_hr": v} for v in (55, 50, 50, 49, 50, 51)])
    calls, profile = [], {"icu_resting_hr": 55}

    class R:
        def __init__(self, j): self.j = j
        def json(self): return self.j
        def raise_for_status(self): pass

    def api(method, **kw):
        calls.append((method, kw.get("json")))
        if method == "PUT":
            profile.update(kw["json"])
        return R(dict(profile))
    monkeypatch.setattr(H, "_api", api)
    assert H.maybe_set(date(2026, 11, 25)) is None and not calls          # Wednesday: not a set day
    e = H.maybe_set(date(2026, 11, 30))                                   # Monday: 55 -> median 50
    assert e["value"] == 50 and e["previous"] == 55 and ("PUT", {"icu_resting_hr": 50}) in calls
    calls.clear()
    assert H.maybe_set(date(2026, 12, 3)) is None                         # Thursday, already 50: no write
    assert [c[0] for c in calls] == ["GET"] and H.last_set()["value"] == 50
    monkeypatch.setattr(H, "query", lambda sql, p=None: [{"resting_hr": 50}] * 3)
    assert H.maybe_set(date(2026, 12, 7)) is None                         # too few readings: leave it


def test_night_data_accepts_the_resting_hr_the_bot_set(monkeypatch, tmp_path):
    import json
    from datetime import date, datetime
    from core import readiness as R, resting_hr as H
    monkeypatch.setattr(H, "STATE_FILE", str(tmp_path / "rhr.json"))
    night = {"sleep_end": datetime(2026, 11, 26, 6, 14), "hrv": 53.0, "resting_hr": 54}   # a high morning
    _night_rows(monkeypatch, R, night, 50)
    assert not R.night_data(date(2026, 11, 26))["ready"]          # 50 vs 54, not ours: could be a placeholder
    json.dump({"value": 50}, open(H.STATE_FILE, "w"))
    assert R.night_data(date(2026, 11, 26))["ready"]              # our steady value: nothing to wait for
