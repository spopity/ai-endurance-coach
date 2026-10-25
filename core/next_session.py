"""
The head coach's "next session" decision after a new activity - made once (all coach
rules, readiness and the injury check apply) and stored on the activity, so the
Strava note, the dashboard and the chat all point to the same session.

Only the most recent activity gets one: once something newer is logged, the old
"next session" is history and nobody should repeat it. Without a new activity the
decision doesn't sit there forever: refresh() re-decides it once it is MAX_AGE_HOURS
old (run after every sync) and every morning before the briefing (with last night's
sleep/HRV), with the days since the last training in the prompt - so a skipped
session turns into a fresh plan instead of waiting to be done.

The athlete can change it from a chat, but never silently: when they ask, the coach
calls propose() (a pending row in next_session_proposals), and the decision is only
replaced when they tap Apply in Telegram or on the dashboard (apply_proposal).
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from core.db import get_connection
from core.overview import query
from core.api_log import trigger
from core.gym import gym_focus

logger = logging.getLogger("AI_COACH_V1.next_session")

MAX_AGE_HOURS = 24        # re-decided after this long without a new activity
MORNING_MIN_AGE_HOURS = 8  # the morning re-check leaves a decision made since last evening alone
REDECIDE_AFTER_HOURS = 12  # an activity this old gets the "re-decide" prompt
RECENT_ACTIVITY_HOURS = 36  # a Strava note still gets a next session for an activity this old

DECIDE_PROMPT = (
    'Decide my NEXT training session after my {type} "{name}" on {day} (activity {external_id}). '
    "Follow all your rules, including readiness, `availability` and the injury check. " + "{answer}"
)
REDECIDE_PROMPT = (
    'Re-decide my NEXT training session now. My last training was my {type} "{name}" on {day} '
    "(activity {external_id}), {ago}. Use today's data: last night's sleep and HRV, readiness, my feedback "
    "(`athlete_feedback`), `availability` (never plan training on a day I can't train; stay within max_minutes on "
    "a limited day; a planned entry on the next training day is that day's session), `return_to_training`, and the sessions still to do this week "
    "(`current_training_block.sessions_remaining`, `days_left_this_week`). If `next_session_decided` is still there, keep it unless something justifies a change. "
    "Follow all your rules. " + "{answer}"
)
ANSWER_FORMAT = (
    "Answer in exactly two short lines and nothing else:\n"
    "Session: <sport, duration and intensity/targets - watts for rides, HR or feel for runs - or rest>\n"
    "Why: <one short reason>\n"
    "Say 'next session', never 'tomorrow' or a weekday."
)


def _cutoff(hours: float = MAX_AGE_HOURS) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def is_current(a: dict, require_recent: bool = True) -> bool:
    """Is `a` the most recent activity (and, with require_recent, recent enough that
    'next session' still means something)?"""
    if require_recent and a["start_time"] < _cutoff(RECENT_ACTIVITY_HOURS):
        return False
    newer = query("SELECT 1 FROM activities WHERE superseded_by IS NULL AND start_time > %s LIMIT 1;",
                  (a["start_time"],))
    return not newer


def _stored(external_id: str) -> str | None:
    """The activity's decision, unless it's stale."""
    rows = query("""
        SELECT next_session FROM activities
        WHERE external_id = %s AND superseded_by IS NULL AND next_session_at >= %s;
    """, (external_id, _cutoff()))
    return rows[0]["next_session"] if rows else None


def get_or_decide(a: dict, require_recent: bool = True, redo: bool = False) -> str | None:
    """The stored decision for activity `a`, deciding it now if needed (always, with `redo`).
    None when `a` isn't the latest activity (or the coach couldn't be reached).
    The dashboard passes require_recent=False: after rest days the latest activity is old, but
    the athlete still wants a next session. Blocking - call via asyncio.to_thread."""
    if not is_current(a, require_recent):
        return None
    stored = None if redo else _stored(a["external_id"])
    if stored:
        return stored
    from core.chat import ask_supervisor_sync  # lazy: core.chat imports the whole agent graph

    age = datetime.now(timezone.utc) - a["start_time"]
    fields = dict(type=a["type"], name=a["name"], day=a["start_time"].date().isoformat(),
                  external_id=a["external_id"], answer=ANSWER_FORMAT)
    if age > timedelta(hours=REDECIDE_AFTER_HOURS):
        days = age.days
        ago = f"{days} day{'s' if days != 1 else ''} ago" if days else f"{age.seconds // 3600} hours ago"
        prompt = REDECIDE_PROMPT.format(ago=ago, **fields)
    else:
        prompt = DECIDE_PROMPT.format(**fields)
    if gym := gym_focus(a["type"], a["name"]):
        prompt += f"\nThat gym session: {gym}."
    try:
        with trigger("next_session"):
            _, text = ask_supervisor_sync(prompt, agent="ai_coach")
    except Exception as e:
        logger.error(f"Next-session decision for {a['external_id']} failed: {e}", exc_info=True)
        return None
    text = text.strip()
    if not text:
        return None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # First decision wins if two processes raced (Strava timer vs dashboard).
            cur.execute("""
                UPDATE activities SET next_session = %s, next_session_at = now(), next_session_source = 'coach'
                WHERE external_id = %s AND superseded_by IS NULL
                  AND (next_session IS NULL OR next_session_at < %s OR %s);
            """, (text, a["external_id"], _cutoff(), redo))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Next session decided after {a['external_id']}: {text!r}")
    return _stored(a["external_id"]) or text


def invalidate():
    """Marks the current decision stale (availability or check-in changed), so refresh() and
    the dashboard decide afresh - an athlete-applied choice included, since it predates the change."""
    a = latest_activity()
    if not a or not a["next_session"]:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE activities SET next_session_at = %s, next_session_source = 'coach' "
                        "WHERE external_id = %s AND superseded_by IS NULL;",
                        (_cutoff(MAX_AGE_HOURS + 1), a["external_id"]))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Next session after {a['external_id']} marked stale.")


def replan() -> str | None:
    """After the athlete's situation changed (availability, check-in): recompute the week's plan
    (targets shrink for days off) and decide the next session again. Blocking."""
    from core.periodization import update_plan  # lazy: only needed here
    conn = get_connection()
    try:
        update_plan(conn)
    finally:
        conn.close()
    invalidate()
    return refresh()


def refresh(morning: bool = False) -> str | None:
    """Re-decides the next session when it has gone stale without a new activity: none yet,
    older than MAX_AGE_HOURS, or - with `morning` - made before last night. A choice the
    athlete applied themselves is kept for a full MAX_AGE_HOURS even in the morning.
    Returns the new decision, or None when nothing needed (or could get) a new one.
    Blocking - call via asyncio.to_thread."""
    a = latest_activity()
    if not a:
        return None
    if a["next_session"] and a["next_session_at"]:
        age = datetime.now(timezone.utc) - a["next_session_at"]
        limit = MAX_AGE_HOURS if not morning or a["next_session_source"] == "athlete" else MORNING_MIN_AGE_HOURS
        if age < timedelta(hours=limit):
            return None
    logger.info(f"Next session stale after {a['external_id']} - re-deciding{' (morning)' if morning else ''}.")
    return get_or_decide(a, require_recent=False, redo=True)


def session_line(decision: str) -> str:
    """Just the session, without the (possibly private) reason - for public text."""
    m = re.search(r"session:\s*(.+)", decision, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return "\n".join(l for l in decision.splitlines() if not l.strip().lower().startswith("why")).strip()


def latest_activity() -> dict | None:
    rows = query("""
        SELECT external_id, name, type, start_time, moving_time_sec, next_session, next_session_at,
               next_session_source FROM activities
        WHERE superseded_by IS NULL ORDER BY start_time DESC LIMIT 1;
    """)
    return rows[0] if rows else None


def latest_decision() -> dict | None:
    """The decision after the most recent activity, if one was made and isn't stale - for the
    chat's context and the dashboard, so nothing silently contradicts it."""
    a = latest_activity()
    if not a or not a["next_session"] or a["next_session_at"] < _cutoff():
        return None
    return {"after_activity": f"{a['type']} '{a['name']}' {a['start_time']:%Y-%m-%d}",
            "after_activity_id": a["external_id"],
            "decided_at": a["next_session_at"].isoformat(timespec="minutes"),
            "decided_by": a["next_session_source"] or "coach",
            "decision": a["next_session"]}


def why_line(decision: str) -> str | None:
    m = re.search(r"why:\s*(.+)", decision, re.IGNORECASE | re.DOTALL)
    return " ".join(m.group(1).split()) if m else None


# --- Changes agreed in chat: propose, then the athlete applies or discards ---

PROPOSAL_MAX_AGE_HOURS = 24


class ProposalError(Exception):
    """A proposal that can't be applied (gone, already resolved, or overtaken by a new activity)."""


def propose(turn_id: str, session: str, why: str = "") -> dict:
    """Stores a pending change to the next session. Changes nothing the athlete sees until
    apply_proposal(). Older pending proposals are discarded - only the latest one counts."""
    a = latest_activity()
    if not a:
        raise ProposalError("There is no activity yet, so there is no next session to change.")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE next_session_proposals SET status = 'discarded', resolved_at = now() "
                        "WHERE status = 'pending';")
            cur.execute("""
                INSERT INTO next_session_proposals (turn_id, after_activity_id, session, why)
                VALUES (%s, %s, %s, %s) RETURNING id;
            """, (turn_id, a["external_id"], session.strip(), (why or "").strip() or None))
            pid = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Next-session change proposed (#{pid}, turn {turn_id}): {session!r}")
    return {"id": pid, "session": session.strip(), "why": (why or "").strip() or None}


def proposal_for_turn(turn_id: str) -> dict | None:
    """The pending proposal a chat turn made, for the front-end to show Apply/Discard."""
    rows = query("""
        SELECT id, session, why FROM next_session_proposals
        WHERE turn_id = %s AND status = 'pending' ORDER BY id DESC LIMIT 1;
    """, (turn_id,))
    return dict(rows[0]) if rows else None


def _resolve(pid: int, status: str) -> dict:
    rows = query("SELECT * FROM next_session_proposals WHERE id = %s;", (pid,))
    if not rows:
        raise ProposalError("That proposal doesn't exist.")
    p = rows[0]
    if p["status"] != "pending":
        raise ProposalError(f"That proposal was already {p['status']}.")
    if status == "applied":
        if p["created_at"] < datetime.now(timezone.utc) - timedelta(hours=PROPOSAL_MAX_AGE_HOURS):
            raise ProposalError("That proposal is over a day old - ask the coach again.")
        a = latest_activity()
        if not a or a["external_id"] != p["after_activity_id"]:
            raise ProposalError("A new activity was logged since, so the coach has decided a new next "
                                "session - ask again if you still want a change.")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if status == "applied":
                decision = f"Session: {p['session']}" + (f"\nWhy: {p['why']}" if p["why"] else "")
                cur.execute("""
                    UPDATE activities SET next_session = %s, next_session_at = now(), next_session_source = 'athlete'
                    WHERE external_id = %s AND superseded_by IS NULL;
                """, (decision, p["after_activity_id"]))
            cur.execute("UPDATE next_session_proposals SET status = %s, resolved_at = now() "
                        "WHERE id = %s AND status = 'pending';", (status, pid))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Next-session proposal #{pid} {status}: {p['session']!r}")
    return {"id": pid, "session": p["session"], "why": p["why"], "status": status}


def apply_proposal(pid: int) -> dict:
    """Makes the proposed session THE next session (dashboard, briefing, Strava, chat)."""
    return _resolve(pid, "applied")


def discard_proposal(pid: int) -> dict:
    return _resolve(pid, "discarded")
