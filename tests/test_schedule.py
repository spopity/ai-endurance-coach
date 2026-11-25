"""The rolling 7-day plan (core/schedule.py): the hard/easy rules and how it adapts."""
from datetime import date, timedelta

from conftest import BUILD_MIX, MONDAY, day_index, done, entry, kinds

from core.schedule import SPORT

# Spelled out here, not imported: a test must not share the definition it checks.
HARD = {"bike_q", "run_q", "gym_legs"}


def hard_days(plan) -> list[date]:
    return [date.fromisoformat(d["date"]) for d in plan["days"] if any(s["kind"] in HARD for s in d["sessions"])]


def all_kinds(plan) -> list[str]:
    return [k for day in kinds(plan) for k in day]


def test_full_week_has_the_mix_and_spacing(week):
    plan = week()
    ks = all_kinds(plan)
    assert sum(SPORT[k] == "ride" for k in ks) == 4
    assert sum(SPORT[k] == "run" for k in ks) == 3
    assert sum(SPORT[k] == "gym" for k in ks) == 2
    assert ks.count("bike_q") == 2 and ks.count("run_q") == 1
    assert ks.count("long_ride") == 1 and ks.count("long_run") == 1
    hd = hard_days(plan)
    assert all((b - a).days > 1 for a, b in zip(hd, hd[1:])), f"hard days back to back: {hd}"
    assert not plan["dropped"]


def test_one_endurance_session_a_day_and_long_ones_at_the_weekend(week):
    plan = week()
    for d in plan["days"]:
        assert sum(SPORT[s["kind"]] != "gym" for s in d["sessions"]) <= 1, d
    weekend = {d["weekday"]: kinds(plan)[i] for i, d in enumerate(plan["days"]) if d["weekday"] in ("Sat", "Sun")}
    assert "long_ride" in sum(weekend.values(), [])


def test_heavy_legs_on_a_hard_day(week):
    plan = week()
    for d in plan["days"]:
        ks = [s["kind"] for s in d["sessions"]]
        if "gym_legs" in ks:
            assert any(k in ("bike_q", "run_q") for k in ks), ks


def test_skipped_hard_ride_moves_on(week):
    """Tuesday's sweet spot was skipped: on Wednesday both bike quality sessions are still to do."""
    wed = MONDAY + timedelta(days=2)
    plan = week(today=wed, done=[done(MONDAY, "run")])
    first_week = [k for d in plan["days"] if date.fromisoformat(d["date"]) < MONDAY + timedelta(days=7)
                  for k in (s["kind"] for s in d["sessions"])]
    assert first_week.count("bike_q") == 2
    assert not any(x["session"] == "Bike quality" for x in plan["dropped"])


def test_done_session_is_not_planned_again(week):
    plan = week(today=MONDAY + timedelta(days=1), done=[done(MONDAY, "bike_q", flavor="vo2")])
    first_week = [k for d in plan["days"] if date.fromisoformat(d["date"]) < MONDAY + timedelta(days=7)
                  for k in (s["kind"] for s in d["sessions"])]
    assert first_week.count("bike_q") == 1


def test_no_hard_session_the_day_after_a_hard_one(week):
    tue = MONDAY + timedelta(days=1)
    plan = week(today=tue, done=[done(MONDAY, "bike_q")])
    assert not any(s["kind"] in HARD for s in plan["days"][0]["sessions"])


def test_trained_today_starts_tomorrow(week):
    plan = week(done=[done(MONDAY, "ride")])
    assert plan["days"][0]["date"] == (MONDAY + timedelta(days=1)).isoformat()


def test_red_readiness_keeps_day_one_short_and_easy(week):
    plan = week(readiness={"level": "red"})
    d1 = plan["days"][0]
    assert not any(s["kind"] in HARD for s in d1["sessions"])
    assert d1["max_minutes"] == 45
    assert all(s["kind"] not in ("long_ride", "long_run") for s in d1["sessions"])


def test_amber_notes_the_hard_session(week):
    plan = week(readiness={"level": "amber"})
    first_hard = next(s for d in plan["days"][:1] for s in d["sessions"] if s["kind"] in ("bike_q", "run_q"))
    assert "warm-up test" in first_hard["note"]


def test_running_limit_turns_quality_runs_easy(week):
    plan = week(readiness={"level": "green", "running": {"limit": "no_hard_run"}})
    assert "run_q" not in all_kinds(plan)


def test_sick_days_get_nothing(week):
    sick = [MONDAY + timedelta(days=i) for i in range(3)]
    plan = week(entries=[entry("sick", sick)])
    for i in range(3):
        assert plan["days"][i]["sessions"] == []
        assert plan["days"][i]["status"] == "sick"


def test_limited_day_only_gets_what_fits(week):
    fri = MONDAY + timedelta(days=4)
    plan = week(entries=[entry("limited", [fri], max_minutes=45)])
    for s in plan["days"][day_index(plan, fri)]["sessions"]:
        assert s["kind"] not in ("long_ride", "long_run")


def test_lost_days_drop_filler_first(week):
    """Only 3 days available: the key sessions stay, the easy filler goes."""
    away = [MONDAY + timedelta(days=i) for i in range(4)]
    plan = week(entries=[entry("away", away)])
    dropped = [x["session"] for x in plan["dropped"] if x["week"] == MONDAY.isoformat()]
    kept = [k for d in plan["days"][:7] for k in (s["kind"] for s in d["sessions"])]
    assert {"Easy ride", "Easy run"} <= set(dropped), "the easy filler goes first"
    assert "bike_q" in kept and "run_q" in kept and "long_ride" in kept, "one quality per sport + the long ride stay"
    hd = hard_days(plan)
    assert all((b - a).days > 1 for a, b in zip(hd, hd[1:]))


def test_pinned_session_takes_its_day(week):
    wed = MONDAY + timedelta(days=2)
    plan = week(entries=[entry("planned", [wed], session="Zwift race 60 min")])
    d = plan["days"][day_index(plan, wed)]
    assert any(s["pinned"] and s["detail"] == "Zwift race 60 min" for s in d["sessions"])


def test_deload_week_is_lighter(week):
    plan = week(mix={"endurance": 3, "run": 2, "weights": 2}, phase="Build", deload=True)
    ks = [k for d in plan["days"][:7] for k in (s["kind"] for s in d["sessions"])
          if date.fromisoformat(d["date"]) < MONDAY + timedelta(days=7)]
    assert "bike_q" not in ks and "run_q" not in ks


def test_quality_run_counts_as_hard(week):
    """A quality run the day after a bike quality session would be two hard days in a row."""
    plan = week()
    for a, b in zip(plan["days"], plan["days"][1:]):
        ka, kb = {s["kind"] for s in a["sessions"]}, {s["kind"] for s in b["sessions"]}
        assert not (ka & {"bike_q", "run_q"} and kb & {"bike_q", "run_q"}), (a["date"], ka, kb)


# --- planned vs done (Intervals.icu compliance) ---

def _row(type_, mins, paired=None, compliance=None, name="x"):
    return {"day": MONDAY, "type": type_, "name": name, "moving_time_sec": mins * 60, "pz": None, "hz": None,
            "paired": paired, "compliance": compliance}


def _done_with(monkeypatch, rows):
    from core import schedule, workouts
    monkeypatch.setattr(schedule, "query", lambda *a, **k: rows)
    monkeypatch.setattr(workouts, "pushed_events", lambda: {
        111: {"date": MONDAY.isoformat(), "kind": "bike_q", "name": "Threshold 3x10 min (AI coach)", "description": ""},
        222: {"date": MONDAY.isoformat(), "kind": "long_ride", "name": "Long ride (AI coach)", "description": ""}})
    return schedule._done(MONDAY, MONDAY)


def test_completed_planned_workout_counts_as_planned(monkeypatch):
    """No zone data at all, but 92% of the pushed threshold workout: it's the bike quality session."""
    assert _done_with(monkeypatch, [_row("Ride", 70, "111", "92.0")])[0]["kind"] == "bike_q"


def test_long_ride_a_bit_short_still_counts(monkeypatch):
    assert _done_with(monkeypatch, [_row("Ride", 100, "222", "85")])[0]["kind"] == "long_ride"


def test_incomplete_planned_workout_falls_back_to_what_was_done(monkeypatch):
    """45% of the threshold workout and no hard zone time: an easy ride, the quality is still to do."""
    assert _done_with(monkeypatch, [_row("Ride", 40, "111", "45")])[0]["kind"] == "ride"


def test_unknown_or_other_sport_pairing_is_ignored(monkeypatch):
    assert _done_with(monkeypatch, [_row("Ride", 40, "999", "100")])[0]["kind"] == "ride"
    assert _done_with(monkeypatch, [_row("Run", 40, "111", "100")])[0]["kind"] == "run"


# --- tests and long-run progression ---

DELOAD_MIX = {"endurance": 3, "run": 2, "weights": 2}
LAST_WEEK = MONDAY - timedelta(days=7)


def first_week(plan) -> list[str]:
    return [k for d in plan["days"] if date.fromisoformat(d["date"]) < MONDAY + timedelta(days=7)
            for k in (s["kind"] for s in d["sessions"])]


def test_ftp_test_at_the_end_of_the_recovery_week(week):
    plan = week(deload=True, phase="Recovery", mix=DELOAD_MIX, last_ftp_test=MONDAY - timedelta(days=40),
                weeks={LAST_WEEK: ("Build", False, BUILD_MIX)})
    day = next(d for d in plan["days"] if any(s["kind"] == "ftp_test" for s in d["sessions"]))
    assert date.fromisoformat(day["date"]).weekday() >= 4      # Fri-Sun: fresh legs after the easy week
    assert "bike_q" not in all_kinds(plan)


def test_recent_ftp_test_skips_a_recovery_week(week):
    plan = week(deload=True, phase="Recovery", mix=DELOAD_MIX, last_ftp_test=MONDAY - timedelta(days=25),
                weeks={LAST_WEEK: ("Build", False, BUILD_MIX)})
    assert "ftp_test" not in all_kinds(plan)


def test_no_test_in_an_ordinary_loading_week_unless_late(week):
    assert "ftp_test" not in first_week(week(last_ftp_test=MONDAY - timedelta(days=50)))
    late = first_week(week(last_ftp_test=MONDAY - timedelta(days=60)))   # 66 days by Sunday: can't wait
    assert late.count("ftp_test") == 1 and late.count("bike_q") == 1


def test_cs_test_is_the_first_quality_run_after_recovery(week):
    after = {LAST_WEEK: ("Recovery", True, DELOAD_MIX)}
    plan = week(last_run_test=MONDAY - timedelta(days=60), weeks=after)
    assert "run_test" in first_week(plan) and "run_q" not in first_week(plan)
    limited = week(last_run_test=None, weeks=after, readiness={"level": "green", "running": {"limit": "no_hard_run"}})
    assert "run_test" not in all_kinds(limited)


def test_no_test_soon_after_illness(week):
    plan = week(last_run_test=None, weeks={LAST_WEEK: ("Recovery", True, DELOAD_MIX)},
                entries=[entry("sick", [MONDAY - timedelta(days=5)])])
    assert "run_test" not in first_week(plan)


def test_amber_keeps_the_test_off_day_one_only(week):
    plan = week(last_run_test=None, weeks={LAST_WEEK: ("Recovery", True, DELOAD_MIX)},
                readiness={"level": "amber"})
    assert "run_test" not in [s["kind"] for s in plan["days"][0]["sessions"]]
    assert "run_test" in first_week(plan)


def test_one_test_a_week(week):
    # The FTP test was missed at the end of last week's recovery week (catch-up) and the CS test's
    # slot is this week too: the FTP test goes first, the CS test moves to next week.
    plan = week(last_ftp_test=MONDAY - timedelta(days=45), last_run_test=None,
                weeks={LAST_WEEK: ("Recovery", True, DELOAD_MIX)})
    assert first_week(plan).count("ftp_test") == 1 and "run_test" not in first_week(plan)


def test_no_test_in_a_taper(week):
    plan = week(phase="Taper", last_ftp_test=MONDAY - timedelta(days=80), last_run_test=None)
    assert not {"ftp_test", "run_test"} & set(first_week(plan))


def test_done_test_counts_as_the_weeks_quality(week):
    plan = week(today=MONDAY + timedelta(days=1), done=[done(MONDAY, "ftp_test")], last_ftp_test=MONDAY)
    assert first_week(plan).count("bike_q") == 1 and "ftp_test" not in first_week(plan)


def test_long_run_grows_about_ten_percent():
    from core.schedule import long_run_minutes
    assert long_run_minutes(70, False) == "75-80 min"
    assert long_run_minutes(30, False) == "45-50 min"      # floor
    assert long_run_minutes(130, False) == "120-125 min"   # cap
    assert long_run_minutes(70, True) == "50-55 min"       # deload: 75%
    assert long_run_minutes(None, False) == "60-75 min"


def test_long_run_detail_uses_the_progression(week):
    plan = week(longest_run_min=70)
    lr = next(s for d in plan["days"] for s in d["sessions"] if s["kind"] == "long_run")
    assert lr["detail"].startswith("75-80 min")


# --- two sessions on one day: the first one done doesn't rewrite the day ---

def _double_day(week):
    """A morning plan of the week with a bike quality + heavy legs day; returns (that day, plan)."""
    for i in range(7):
        today = MONDAY + timedelta(days=i)
        plan = week(today=today)
        if set(kinds(plan)[0]) == {"bike_q", "gym_legs"}:
            return today, plan
    raise AssertionError("no bike quality + heavy legs day in the plan")


def test_second_session_stays_after_the_first_is_done(week, today_plan):
    today, _ = _double_day(week)
    assert sorted(today_plan[today]) == ["bike_q", "gym_legs"]   # kept from the morning build
    plan = week(today=today, done=[done(today, "bike_q", flavor="vo2")])
    assert plan["days"][0]["date"] == today.isoformat()
    assert kinds(plan)[0] == ["gym_legs"]
    week_kinds = [k for d in plan["days"] if date.fromisoformat(d["date"]) < MONDAY + timedelta(days=7)
                  for k in (s["kind"] for s in d["sessions"])]
    assert week_kinds.count("gym_legs") == 1                       # not planned again later
    assert not any(s in HARD for s in kinds(plan)[1])                # today still counts as hard


def test_other_session_done_instead_keeps_the_gym(week):
    today, _ = _double_day(week)
    plan = week(today=today, done=[done(today, "ride")])
    assert kinds(plan)[0] == ["gym_legs"]                            # no second endurance session today


def test_gym_done_first_keeps_the_ride(week):
    today, _ = _double_day(week)
    plan = week(today=today, done=[done(today, "gym_legs", name="Legs")])
    assert kinds(plan)[0] == ["bike_q"]


def test_nothing_new_goes_on_today(week, today_plan):
    today = MONDAY + timedelta(days=2)
    today_plan[today] = ["ride"]
    plan = week(today=today, done=[done(today, "ride")])
    assert plan["days"][0]["date"] == today.isoformat()
    assert kinds(plan)[0] == [] and plan["days"][0]["rest"]


def test_red_readiness_notes_the_kept_session(week):
    today, _ = _double_day(week)
    plan = week(today=today, done=[done(today, "bike_q", flavor="vo2")], readiness={"level": "red"})
    assert "red readiness" in plan["days"][0]["sessions"][0]["note"]


def test_pin_kind_first_sport_and_negations():
    from core.schedule import pin_kind
    assert pin_kind("Bike quality only: Threshold 3x10 min inside 45 min. No gym.") == "bike_q"
    assert pin_kind("Gym - heavy legs, 16:00 or later (6+ h after the VO2max ride)") == "gym_legs"
    assert pin_kind("Upper body gym") == "gym_upper"
    assert pin_kind("Easy run 40 min, no bike") == "run"
    assert pin_kind("Long ride 3 h") == "long_ride"
