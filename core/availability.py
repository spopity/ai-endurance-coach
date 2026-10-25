"""
The athlete's availability: days they can't train (sick, away, no time) or can only
train for a limited time. Stated in a chat ("I'm sick, no training for 3 days", "I'm
away until Sunday", "only 45 min today") and saved by the coach's record_availability
tool as exact dates - "3 days" from today means today and the next 2 dates.

Kept in state/availability.json (it is the athlete's own schedule, not training data):

    {"updated": "2026-10-25",
     "entries": [{"id": "...", "kind": "sick", "dates": ["2026-10-25", "2026-10-26", ...],
                  "max_minutes": null, "note": "flu", "created_at": "..."}],
     "history": [...entries whose dates have all passed...]}

The file keeps itself current: every read moves entries whose dates have all passed to
`history` (kept HISTORY_DAYS for the plan replay and the after-illness return), so when
the dates are over, normal training resumes with no action from the athlete.

Kinds: sick (no training; the first sessions back are easy), away / unavailable (no
training), limited (training possible, at most max_minutes per day), and planned: a
session the athlete pinned to a date ("long ride on Saturday instead of Tuesday") -
the coach prescribes it on that day unless it's unsafe. A swap is two planned entries.
A no-training entry on a date removes a planned session there; a planned entry only
replaces another planned one (it never cancels a sick or away day).
"""
import fcntl
import json
import logging
import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger("AI_COACH_V1.availability")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "state", "availability.json")
LOCK_PATH = PATH + ".lock"

KINDS = {
    "sick": "ill - no training",
    "away": "away - no training",
    "unavailable": "no time - no training",
    "limited": "limited time",
    "planned": "planned session",
}
NO_TRAINING = {"sick", "away", "unavailable"}
MAX_DAYS = 60
HISTORY_DAYS = 400  # the plan replays the year


@contextmanager
def _locked():
    """One writer at a time (the app and the Telegram bot are separate processes)."""
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(LOCK_PATH, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read_raw() -> dict:
    try:
        with open(PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except ValueError:
        logger.error(f"{PATH} is not valid JSON - starting from an empty availability file.")
        data = {}
    return {"updated": data.get("updated"), "entries": data.get("entries", []), "history": data.get("history", [])}


def _write_raw(data: dict):
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, PATH)


def _housekeep(data: dict, today: date) -> bool:
    """Moves entries whose dates have all passed to history and drops very old history.
    Returns True if anything changed."""
    changed = data.get("updated") != today.isoformat()
    keep = []
    for e in data["entries"]:
        if e["dates"] and max(e["dates"]) < today.isoformat():
            data["history"].append(e)
            changed = True
            logger.info(f"Availability entry {e['id']} ({e['kind']} {e['dates'][0]}..{e['dates'][-1]}) is over.")
        else:
            keep.append(e)
    data["entries"] = keep
    cutoff = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    old = len(data["history"])
    data["history"] = [e for e in data["history"] if e["dates"] and max(e["dates"]) >= cutoff]
    data["updated"] = today.isoformat()
    return changed or len(data["history"]) != old


def load(today: date | None = None) -> dict:
    """The file, brought up to date (and rewritten if the date moved on)."""
    today = today or date.today()
    with _locked():
        data = _read_raw()
        if _housekeep(data, today):
            _write_raw(data)
    return data


def add(kind: str, start: date, days: int, max_minutes: int | None = None, note: str = "",
        session: str | None = None) -> dict:
    """Saves `days` consecutive dates from `start`. A new entry replaces the same dates of
    older entries (the latest thing the athlete said wins) - a planned one only those of
    other planned entries. `session` (planned only): what to do that day."""
    if kind not in KINDS:
        raise ValueError(f"Unknown kind {kind!r} - one of {', '.join(KINDS)}")
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be 1-{MAX_DAYS}")
    if kind == "limited" and not max_minutes:
        raise ValueError("limited needs max_minutes")
    if kind == "planned" and not (session or "").strip():
        raise ValueError("planned needs the session")
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    entry = {"id": uuid.uuid4().hex[:8], "kind": kind, "dates": dates,
             "max_minutes": int(max_minutes) if kind == "limited" else None,
             **({"session": session.strip()} if kind == "planned" else {}),
             "note": (note or "").strip(), "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with _locked():
        data = _read_raw()
        if kind == "planned":
            blocked = {d for e in data["entries"] if e["kind"] in NO_TRAINING for d in e["dates"]}
            if clash := [d for d in dates if d in blocked]:
                raise ValueError(f"{', '.join(clash)} is saved as a no-training day - cancel that first")
        for e in data["entries"]:
            if kind != "planned" or e["kind"] == "planned":
                e["dates"] = [d for d in e["dates"] if d not in dates]
        data["entries"] = [e for e in data["entries"] if e["dates"]] + [entry]
        _housekeep(data, date.today())
        _write_raw(data)
    logger.info(f"Availability saved: {kind} {dates[0]}..{dates[-1]}" + (f" (max {max_minutes} min)" if max_minutes else "")
                + (f": {session}" if kind == "planned" else ""))
    return entry


def cancel(entry_id: str | None = None, from_date: date | None = None) -> list[dict]:
    """Ends an entry (or, without entry_id, every current entry) from `from_date` (default
    today): "I'm better", "the trip is cancelled". Dates before from_date are kept, since
    they happened. Returns the entries that changed."""
    from_iso = (from_date or date.today()).isoformat()
    changed = []
    with _locked():
        data = _read_raw()
        for e in data["entries"]:
            if entry_id and e["id"] != entry_id:
                continue
            kept = [d for d in e["dates"] if d < from_iso]
            if kept != e["dates"]:
                e["dates"] = kept
                changed.append(e)
        data["entries"] = [e for e in data["entries"] if e["dates"]]  # past dates kept move to history below
        _housekeep(data, date.today())
        _write_raw(data)
    logger.info(f"Availability cancelled from {from_iso}: {[e['id'] for e in changed]}")
    return changed


def _all_entries(data: dict) -> list[dict]:
    return data["entries"] + data["history"]


def day_status(d: date, data: dict | None = None) -> dict | None:
    """The availability entry covering day d (the latest one wins; planned sessions aren't
    availability), or None for a normal day."""
    data = data or load()
    iso = d.isoformat()
    hits = [e for e in _all_entries(data) if iso in e["dates"] and e["kind"] != "planned"]
    return max(hits, key=lambda e: e["created_at"]) if hits else None


def planned_session(d: date, data: dict | None = None) -> dict | None:
    """The session pinned to day d, if any."""
    data = data or load()
    hits = [e for e in data["entries"] if e["kind"] == "planned" and d.isoformat() in e["dates"]]
    return max(hits, key=lambda e: e["created_at"]) if hits else None


def unavailable_days(start: date, end: date, data: dict | None = None) -> set[date]:
    """Days in [start, end] with no training possible (sick / away / unavailable)."""
    data = data or load()
    out = set()
    for e in _all_entries(data):
        if e["kind"] in NO_TRAINING:
            out |= {date.fromisoformat(d) for d in e["dates"] if start.isoformat() <= d <= end.isoformat()}
    return out


def last_illness_end(today: date, data: dict | None = None) -> date | None:
    """Last sick day before today, if the athlete was ill within the last 2 weeks."""
    data = data or load()
    days = [date.fromisoformat(d) for e in _all_entries(data) if e["kind"] == "sick" for d in e["dates"]]
    past = [d for d in days if today - timedelta(days=14) <= d < today]
    return max(past) if past else None


def context(today: date | None = None) -> dict:
    """For the coach: today's status and everything coming up, with exact dates."""
    today = today or date.today()
    data = load(today)
    status = day_status(today, data)

    def describe(e: dict) -> dict:
        upcoming = [d for d in e["dates"] if d >= today.isoformat()]
        return {"id": e["id"], "kind": e["kind"], "meaning": KINDS[e["kind"]], "dates": upcoming,
                "from": upcoming[0], "until": upcoming[-1],
                **({"max_minutes": e["max_minutes"]} if e.get("max_minutes") else {}),
                **({"session": e["session"]} if e.get("session") else {}),
                **({"note": e["note"]} if e.get("note") else {})}

    upcoming = [describe(e) for e in sorted(data["entries"], key=lambda e: e["dates"][0])
                if any(d >= today.isoformat() for d in e["dates"])]
    blocked = sorted({d for e in upcoming if e["kind"] in NO_TRAINING for d in e["dates"]})
    next_free = today
    while next_free.isoformat() in blocked:
        next_free += timedelta(days=1)
    planned = planned_session(today, data)
    return {
        "today": (f"{KINDS[status['kind']]}" + (f", at most {status['max_minutes']} min" if status.get("max_minutes") else "")
                  if status else "available") + (f" - planned: {planned['session']}" if planned else ""),
        "entries": upcoming,
        "next_training_day": next_free.isoformat(),
        "illness_ended": (d.isoformat() if (d := last_illness_end(today, data)) and not (status and status["kind"] == "sick") else None),
    }
