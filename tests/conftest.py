"""Shared fixtures. The tests never touch the database, the state files or any external API:
everything the code would read is patched in (see `week` below)."""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import schedule  # noqa: E402

# A dual-sport Build week: 4 rides (1 sweet spot, 1 VO2max, 2 endurance), 3 runs, 2 gym.
BUILD_MIX = {"endurance": 2, "sst": 1, "vo2": 1, "run": 3, "weights": 2}
MONDAY = date(2026, 11, 23)


def entry(kind: str, days: list[date], max_minutes: int | None = None, session: str | None = None) -> dict:
    e = {"id": kind + days[0].isoformat(), "kind": kind, "dates": [d.isoformat() for d in days],
         "max_minutes": max_minutes, "note": "", "created_at": "2026-11-01T00:00:00+00:00"}
    if session:
        e["session"] = session
    return e


@pytest.fixture(autouse=True)
def _no_ftp_lookup(monkeypatch):
    """Workouts scale % targets to the training FTP from the database - not in tests."""
    from core import workouts
    monkeypatch.setattr(workouts, "_ftp_scale", lambda: 1.0)
    monkeypatch.setattr(workouts, "_weight", lambda: 70.0)


@pytest.fixture
def week(monkeypatch):
    """Configures the schedule's inputs; returns a function (today, done, entries, mix, phase,
    deload, kind) -> schedule.build() result."""
    def run(today: date = MONDAY, done: list[dict] | None = None, entries: list[dict] | None = None,
            mix: dict | None = None, phase: str = "Build", deload: bool = False, kind: str = "build_2",
            readiness: dict | None = None, last_ftp_test: date | None = date(2026, 11, 10),
            last_run_test: date | None = date(2026, 11, 10), longest_run_min: float | None = None,
            last_durability_test: date | None = date(2026, 11, 10)):
        block = {"phase": phase, "is_deload": deload, "primary_focus": "test", "sessions": dict(mix or BUILD_MIX)}
        monkeypatch.setattr(schedule.availability, "load", lambda *a, **k: {"entries": entries or [], "history": []})
        monkeypatch.setattr(schedule, "_week_kind", lambda monday: (kind, dict(block, sessions=dict(block["sessions"]))))
        monkeypatch.setattr(schedule, "_done", lambda start, end: [d for d in (done or []) if start <= d["day"] <= end])
        monkeypatch.setattr(schedule, "_targets", lambda: {
            "ftp": 280.0, "hr": [141, 149, 157, 166], "last_ftp_test": last_ftp_test,
            "last_run_test": last_run_test, "longest_run_min": longest_run_min,
            "last_durability_test": last_durability_test, "weight": 70.0})
        monkeypatch.setattr(schedule, "get_focus", lambda: {"mode": "dual"})
        return schedule.build(today=today, readiness=readiness or {"level": "green"})
    return run


def done(day: date, kind: str, name: str = "x", minutes: int = 60, flavor: str | None = None) -> dict:
    d = {"day": day, "kind": kind, "name": name, "minutes": minutes}
    if flavor:
        d["flavor"] = flavor
    return d


def kinds(plan: dict) -> list[list[str]]:
    return [[s["kind"] for s in d["sessions"]] for d in plan["days"]]


def day_index(plan: dict, d: date) -> int:
    return next(i for i, x in enumerate(plan["days"]) if x["date"] == d.isoformat())


__all__ = ["BUILD_MIX", "MONDAY", "entry", "done", "kinds", "day_index", "timedelta"]
