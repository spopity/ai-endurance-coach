"""The test calendar (core/testing.py): tests placed by the long-term plan."""
from datetime import date, timedelta

from core.testing import CS, FTP, plan

MONDAY = date(2026, 11, 23)


def season(pattern: str, phases: str | None = None, start: date = MONDAY - timedelta(weeks=2)) -> list[dict]:
    """'LLLRLLLR' -> weeks (L loading, R recovery); `phases` one letter per week (B base, U build,
    T taper), Build by default."""
    names = {"B": "Base", "U": "Build", "T": "Taper", "P": "Race prep"}
    return [{"monday": start + timedelta(weeks=i), "deload": c == "R",
             "phase": "Recovery" if c == "R" else names[(phases or "U" * len(pattern))[i]]}
            for i, c in enumerate(pattern)]


def placed(planned: dict, test: str) -> list[date]:
    return sorted(m for m, items in planned.items() for t, _ in items if t == test)


def test_ftp_tests_land_on_recovery_weeks_every_second_cycle():
    weeks = season("LLLLRLLLRLLLRLLLRL")   # weeks[0:2] are context
    planned = plan(weeks, {FTP: MONDAY - timedelta(days=30), CS: MONDAY - timedelta(days=30)})
    ftp = placed(planned, FTP)
    assert all(next(w for w in weeks if w["monday"] == m)["deload"] for m in ftp)
    assert ftp == [MONDAY + timedelta(weeks=2), MONDAY + timedelta(weeks=10)]   # every 8 weeks
    assert all(dict(planned[m])[FTP] == "end_of_recovery" for m in ftp)


def test_cs_test_follows_the_recovery_week():
    weeks = season("LLLLRLLLRL")
    planned = plan(weeks, {FTP: MONDAY - timedelta(days=10), CS: None})
    assert placed(planned, CS) == [MONDAY + timedelta(weeks=3)]
    assert dict(planned[MONDAY + timedelta(weeks=3)])[CS] == "first_loading"


def test_a_new_phase_brings_the_test_forward():
    # Base -> Build after this recovery week: a 3-week-old FTP is retested for the new work.
    weeks = season("LLLRLL", phases="BBB" + "R" + "UU")
    planned = plan(weeks, {FTP: MONDAY - timedelta(days=17), CS: MONDAY})
    assert placed(planned, FTP) == [MONDAY + timedelta(weeks=1)]
    same_phase = plan(season("LLLRLL"), {FTP: MONDAY - timedelta(days=17), CS: MONDAY})
    assert placed(same_phase, FTP) == []


def test_late_test_doesnt_wait_for_a_far_recovery_week():
    weeks = season("LLLLLLR")
    planned = plan(weeks, {FTP: MONDAY - timedelta(days=60), CS: MONDAY})
    assert placed(planned, FTP) == [MONDAY]
    assert dict(planned[MONDAY])[FTP] == "late"


def test_missed_slot_is_caught_up_this_week():
    weeks = season("LRLLL")    # last week was the recovery week, and the FTP test didn't happen
    planned = plan(weeks, {FTP: MONDAY - timedelta(days=45), CS: MONDAY})
    assert dict(planned[MONDAY])[FTP] == "catch_up"


def test_one_test_a_week_and_none_in_a_taper():
    weeks = season("LRLLL")
    planned = plan(weeks, {FTP: MONDAY - timedelta(days=45), CS: None})
    assert [t for t, _ in planned[MONDAY]] == [FTP]
    assert placed(planned, CS) == [MONDAY + timedelta(weeks=1)]
    taper = season("LLTTT", phases="UUTTT")
    assert plan(taper, {FTP: MONDAY - timedelta(days=80), CS: None}) == {}
