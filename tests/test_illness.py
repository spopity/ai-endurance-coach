from datetime import date, timedelta

import pytest

from core import feedback
from core.illness import night_signals, status
from core.readiness import verdict

DAY = date(2026, 3, 20)


def nights(last: dict | None = None, before: dict | None = None, n: int = 30) -> dict:
    """30 normal nights (overnight HR 50-52, HRV 48-52, sleep stress 18-22, breathing 14.4-14.6),
    then `before` on the night before DAY and `last` on DAY."""
    rows = {}
    for i in range(n, -1, -1):
        d = DAY - timedelta(days=i)
        rows[d] = {"avg_sleeping_hr": 50 + i % 3, "resting_hr": None, "hrv": 48 + i % 5,
                   "sleep_stress": 18 + i % 5, "sleep_respiration": 14.4 + (i % 3) / 10}
    if before:
        rows[DAY - timedelta(days=1)].update(before)
    if last:
        rows[DAY].update(last)
    return rows


def test_normal_night_is_quiet():
    assert status(nights(), DAY)["level"] == "none"


def test_one_signal_is_a_note():
    s = status(nights({"hrv": 38}), DAY)
    assert s["level"] == "note" and s["signals"] == ["HRV down (38 ms vs 50)"]


def test_two_systems_is_watch():
    s = status(nights({"avg_sleeping_hr": 58, "sleep_respiration": 15.8}), DAY)
    assert s["level"] == "watch" and len(s["signals"]) == 2


def test_hrv_and_sleep_stress_count_as_one_system():
    assert status(nights({"hrv": 38, "sleep_stress": 40}), DAY)["level"] == "note"


def test_autonomic_two_nights_is_watch_not_warning():
    assert status(nights({"hrv": 38}, before={"hrv": 39}), DAY)["level"] == "watch"


def test_all_three_is_warning():
    s = status(nights({"avg_sleeping_hr": 58, "hrv": 38, "sleep_respiration": 15.8}), DAY)
    assert s["level"] == "warning"


def test_watch_two_nights_with_heart_is_warning():
    bad = {"avg_sleeping_hr": 58, "hrv": 38}
    assert status(nights(bad, before=bad), DAY)["level"] == "warning"


def test_missing_data_drops_the_signal():
    rows = nights({"avg_sleeping_hr": None, "sleep_respiration": None, "hrv": 38})
    assert set(night_signals(rows, DAY)) == {"hrv"}
    assert night_signals({}, DAY) == {}


def test_resting_hr_is_the_fallback():
    rows = nights({"avg_sleeping_hr": None})
    for r in rows.values():
        r["resting_hr"] = r["avg_sleeping_hr"] or 51
    rows[DAY]["resting_hr"] = 60
    assert "heart" in night_signals(rows, DAY)


# --- readiness ---

WATCH = {"level": "watch", "signals": ["overnight heart rate up (58 bpm vs 51)", "HRV down (38 ms vs 50)"],
         "text": "early illness signs (or a rough night)"}


def test_watch_trims_the_day():
    level, text = verdict(0, None, illness=WATCH)
    assert level == "amber" and "early illness signs" in text


def test_watch_plus_feeling_off_is_red():
    level, text = verdict(0, None, subjective=([], ["feeling a bit off"]), illness=WATCH)
    assert level == "red" and "you feel a bit off" in text


def test_watch_does_not_double_count_hrv_into_a_cluster():
    assert verdict(0, {"status": "below"}, illness=WATCH)[0] == "amber"


def test_warning_is_red():
    assert verdict(0, None, illness={**WATCH, "level": "warning", "text": "possible illness"})[0] == "red"


def test_note_changes_nothing():
    assert verdict(0, None, illness={"level": "note", "signals": ["x"], "text": "y"})[0] == "green"


# --- check-in ---

@pytest.fixture
def checkin(monkeypatch):
    def set_(**c):
        monkeypatch.setattr(feedback.availability, "day_status", lambda d: None)
        monkeypatch.setattr(feedback, "recent_sessions", lambda n=5: [])
        monkeypatch.setattr(feedback, "today_checkin", lambda d=None: {"body": "normal", "sleep": "ok",
                                                                         "stress": "normal", "motivation": "normal", **c})
        return feedback.signals(DAY)
    return set_


def test_checkin_signals(checkin):
    assert checkin() == ([], [])
    assert checkin(body="off") == ([], ["feeling a bit off"])
    assert checkin(sleep="poor") == ([], [])
    assert checkin(stress="high") == ([], [])
    assert checkin(sleep="poor", stress="high") == ([], ["poor night and high life stress"])


def test_save_checkin_rejects_unknown_answers():
    with pytest.raises(ValueError):
        feedback.save_checkin(DAY, sleep="great")
    with pytest.raises(ValueError):
        feedback.save_checkin(DAY, mood="low")
