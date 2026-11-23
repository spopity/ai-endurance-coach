"""
When the tests happen: placed by the long-term plan (training_blocks), not by a fixed interval.

A test is worth most when the legs are fresh and its result sets the targets of the block that
follows (Friel: test at the end of the recovery week). So:

- FTP test (ride, 20 min, with an LT1 step warm-up): at the END of a recovery week - fresh
  legs, and the new FTP sets the watts of the next block. Recovery weeks have no intervals, so
  the test is the week's one hard effort.
- CS test (run, 3 + 9 min all-out -> critical speed): the quality run of the FIRST loading week
  after a recovery week - still fresh, and the paces count for that block.
- Due at a slot when the last test is at least MIN_GAP_DAYS old (about every second recovery
  week), or PHASE_GAP_DAYS old when the next block starts a new phase (Base -> Build -> Race
  prep: new kinds of work need new targets). Never tested: the first slot.
- Missed slot (amber/red day, illness, no room): the next week's quality session catches up.
- It can't wait past LATE_DAYS: then it replaces the quality session of a loading week. For the
  FTP that is the 8-week rule (core/metrics.py: after 56 days the targets switch to eFTP) plus
  one recovery week - a few easy days on eFTP don't matter, intervals on it would.
- Never in a taper or race week, and only one test a week (the FTP test goes first).

The rolling 7-day plan (core/schedule.py) asks week_tests() for each week it lays out;
calendar() projects the next weeks for the dashboard, the briefing and the coach. Both are
the same pure function (plan()) over the weeks' (phase, deload), so what the calendar shows is
what the 7-day plan will do - and it moves with the plan when a recovery week moves.
"""
from datetime import date, timedelta

FTP, CS = "ftp_test", "run_test"
MIN_GAP_DAYS = 35
PHASE_GAP_DAYS = 21
LATE_DAYS = {FTP: 63, CS: 70}
NO_TEST_PHASES = {"Taper", "Race"}
CONTEXT_WEEKS = 2       # weeks before the first one, to know where the last recovery week was
TITLE = {FTP: "FTP test", CS: "CS test (run)"}
WHEN = {"end_of_recovery": "end of the recovery week, fresh legs - sets the next block's watts",
        "first_loading": "first week after the recovery week, as its quality run - sets the block's paces",
        "catch_up": "catching up a missed test, as the week's quality session",
        "late": "can't wait for the next recovery week, as the week's quality session"}


def _loading(w: dict) -> bool:
    return not w["deload"] and w["phase"] not in NO_TEST_PHASES


def _phase_change(weeks: list[dict], i: int) -> bool:
    """Does the loading week after week i (a recovery week) start a different phase than the one before it?"""
    before = next((w["phase"] for w in reversed(weeks[:i]) if _loading(w)), None)
    after = next((w["phase"] for w in weeks[i + 1:] if _loading(w)), None)
    return bool(before and after and before != after)


def _slot(test: str, weeks: list[dict], i: int) -> str | None:
    w, prev = weeks[i], weeks[i - 1] if i else None
    if w["phase"] in NO_TEST_PHASES:
        return None
    if test == FTP:
        return "end_of_recovery" if w["deload"] else None
    return "first_loading" if _loading(w) and prev and prev["deload"] else None


def _due(test: str, last: date | None, on: date, phase_change: bool) -> bool:
    if last is None:
        return True
    age = (on - last).days
    return age >= MIN_GAP_DAYS or (phase_change and age >= PHASE_GAP_DAYS)


def plan(weeks: list[dict], last: dict[str, date | None], start: int = CONTEXT_WEEKS) -> dict[date, list[tuple]]:
    """{monday: [(test, reason)]} for weeks[start:]. `weeks`: consecutive weeks oldest first, each
    {monday, phase, deload}; weeks before `start` are context only (where the last slot was).
    Assumes each test is done in the week it is planned for (the projection)."""
    out: dict[date, list[tuple]] = {}
    last = dict(last)
    for test in (FTP, CS):
        for i in range(start, len(weeks)):
            w = weeks[i]
            if w["phase"] in NO_TEST_PHASES:
                continue
            monday, end = w["monday"], w["monday"] + timedelta(days=6)
            taken = any(t for t, _ in out.get(monday, []))
            reason = None
            slot = _slot(test, weeks, i)
            if slot and _due(test, last[test], end, slot == "end_of_recovery" and _phase_change(weeks, i)):
                reason = slot
            elif i == start and _loading(w):
                # The real current week: a slot last week that passed without the test -> catch up now.
                prev_slot = next((j for j in range(i - 1, max(-1, i - 3), -1) if _slot(test, weeks, j)), None)
                if prev_slot is not None and _due(test, last[test], weeks[prev_slot]["monday"] + timedelta(days=6), False) \
                        and (last[test] is None or last[test] < weeks[prev_slot]["monday"]):
                    reason = "catch_up"
            if not reason and _loading(w) and last[test] and (end - last[test]).days > LATE_DAYS[test]:
                reason = "late"
            if not reason:
                continue
            if taken:          # one test a week: this one moves to the next loading week
                if i + 1 < len(weeks) and _loading(weeks[i + 1]) and not out.get(weeks[i + 1]["monday"]):
                    out.setdefault(weeks[i + 1]["monday"], []).append((test, reason))
                    last[test] = weeks[i + 1]["monday"] + timedelta(days=1)
                continue
            out.setdefault(monday, []).append((test, reason))
            last[test] = end - timedelta(days=1) if reason == "end_of_recovery" else monday + timedelta(days=1)
    return out


def week_tests(monday: date, week_meta, last: dict[str, date | None], today: date) -> list[tuple]:
    """[(test, reason)] for the week starting `monday`, projected from the current week (so a
    later week assumes the tests planned before it get done). `week_meta(monday)` -> (phase, deload)."""
    this_monday = today - timedelta(days=today.weekday())
    weeks = []
    for k in range(-CONTEXT_WEEKS, (monday - this_monday).days // 7 + 3):
        m = this_monday + timedelta(weeks=k)
        phase, deload = week_meta(m)
        weeks.append({"monday": m, "phase": phase, "deload": bool(deload)})
    return plan(weeks, last).get(monday, [])


def calendar(weeks: int = 16, today: date | None = None) -> list[dict]:
    """The tests of the next `weeks` weeks from the long-term plan: [{week, test, title, why}]."""
    from core.overview import query
    from core.schedule import _targets
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    first = this_monday - timedelta(weeks=CONTEXT_WEEKS)
    rows = query("""SELECT start_date, end_date, phase, is_deload FROM training_blocks
                    WHERE end_date >= %s AND start_date <= %s ORDER BY start_date;""",
                 (first, this_monday + timedelta(weeks=weeks)))
    meta = []
    for k in range(weeks + CONTEXT_WEEKS):
        m = first + timedelta(weeks=k)
        b = next((r for r in rows if r["start_date"] <= m + timedelta(days=3) <= r["end_date"]), None)
        meta.append({"monday": m, "phase": b["phase"] if b else None, "deload": bool(b and b["is_deload"])})
    t = _targets()
    planned = plan(meta, {FTP: t.get("last_ftp_test"), CS: t.get("last_run_test")})
    return [{"week": m.isoformat(), "test": test, "title": TITLE[test], "why": WHEN[reason]}
            for m in sorted(planned) for test, reason in planned[m]]


def summary(items: list[dict] | None = None) -> str:
    items = calendar() if items is None else items
    if not items:
        return "Test calendar: no test in the coming weeks."
    return "Test calendar (from the long-term plan): " + "; ".join(
        f"{i['title']} in the week of {i['week']} ({i['why']})" for i in items[:4]) + "."
