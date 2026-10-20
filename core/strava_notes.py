"""
Coach notes in Strava activity descriptions: a facts block computed in code
(session type, intervals, power/load, fitness change - nothing about the plan week) plus 2-3
sentences from Gemini, appended below the athlete's own description.

Strava descriptions are public to everyone who can see the activity, so notes
never include wellness, injury, body-weight or FTP-check information. Readiness and
the shared next-session decision (core/next_session.py) are passed privately: they
keep the note's "what's next" in line with the coach, without stating the reasons.

The posted text is stored in activities.strava_note, so a re-post replaces exactly
that part of the description and nothing the athlete wrote. A description containing
#nocoach is left alone.
"""
import logging
import re
from datetime import date, timedelta, timezone

from core import strava
from core.api_log import trigger
from core.db import get_connection
from core.llm import gemini_llm
from core.overview import query
from core.focus import get_focus
from core.next_session import get_or_decide, session_line
from core.gym import gym_focus
from core.plan import RIDE_TYPES, RUN_TYPES, classify
from core.readiness import current_readiness

logger = logging.getLogger("AI_COACH_V1.strava_notes")

MIN_MINUTES = 20          # any sport type gets a note, but not 5-min warm-up walks
MAX_AGE_DAYS = 2           # only fresh activities get a note automatically
MATCH_WINDOW_SEC = 120     # Strava vs Intervals.icu start time
OPT_OUT_TAG = "#nocoach"
# Wait after import so Intervals.icu has recomputed fitness/fatigue.
SETTLE_MINUTES = 15
SIGNATURE = "— AI Coach"

KIND_LABELS = {"endurance": "Endurance", "sst": "Sweet spot", "vo2": "VO2max", "run": "Run", "weights": "Weights"}
RECOVERY_IF = 0.55  # Coggan active recovery: below 55% of FTP

COMMENT_PROMPT = """You are an elite endurance coach writing 2-3 short sentences (max 50 words) under a workout on
Strava, for the athlete and their followers. Knowledgeable, encouraging and specific to this session - witty is
fine, hype is not. Say what the session did physiologically. Never mention the training plan, the week or its
phase (build/deload/recovery week), or session counts. {next_rule}
Plain text only - no markdown, no hashtags, no emojis, no greeting, no sign-off. Use only facts given below; don't
repeat the numbers already in the facts block. The activity name is just the athlete's own title - don't treat it
as a description (except for gym sessions: there it names the muscles worked, see "Gym session" below - never
claim a gym session trained the legs or helps cycling/running power unless it says legs were worked). Never mention HRV, sleep, fatigue scores, readiness, injuries, illness, body weight or FTP tests.

Facts block (already shown above your text):
{facts}

More context:
{context}{private}{athlete_notes}"""

NEXT_RULE = ("End with the next session exactly as the coach decided it (below) - same sport and intensity, never "
             "harder or more exciting - in neutral public words (e.g. \"an easy spin next to soak this up\"), "
             "without the private reasons behind it. Say \"next\", never \"tomorrow\" or a weekday.")
NO_NEXT_RULE = "Don't predict or suggest the next session."

PRIVATE = """

PRIVATE - shapes your tone, never mention it or its reasons:
Athlete readiness: {readiness}.{decision}"""

ATHLETE_NOTES = """

The athlete's own notes about this session - build your text around them (they become public through your
text, so still never mention health, illness, injuries or body weight; turn those into neutral wording like
"a tough day"):
{notes}"""


def _execute(sql: str, params=()):
    """Write helper (core.overview.query is read-only: it never commits)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _fmt_int(v) -> str:
    return f"{v:,.0f}" if v is not None else "?"


def _pace(sec_per_km) -> str | None:
    if not sec_per_km:
        return None
    m, s = divmod(round(sec_per_km), 60)
    return f"{m}:{s:02d}/km"


def load_activity(external_id: str) -> dict | None:
    rows = query("""
        SELECT id, external_id, name, type, start_time, moving_time_sec, distance_m, training_load,
               normalized_power_w, avg_power_w, intensity_factor, avg_hr, decoupling_pct,
               strava_id, strava_note, strava_note_at, strava_note_context,
               raw_payload->'icu_zone_times' AS power_zones, raw_payload->'icu_hr_zone_times' AS hr_zones,
               raw_payload->'interval_summary' AS interval_summary, raw_payload->'icu_achievements' AS achievements,
               raw_payload->>'icu_joules' AS joules,
               COALESCE(raw_payload->>'average_weather_temp', raw_payload->>'average_temp') AS temp_c
        FROM activities WHERE external_id = %s AND superseded_by IS NULL;
    """, (external_id,))
    return rows[0] if rows else None


def pending_activities() -> list[dict]:
    """Fresh activities (any type) that haven't had a note posted (or skipped) yet, oldest first."""
    rows = query("""
        SELECT external_id FROM activities
        WHERE superseded_by IS NULL AND strava_note_at IS NULL
          AND moving_time_sec >= %s AND start_time >= now() - %s * interval '1 day'
          AND created_at <= now() - %s * interval '1 minute'
        ORDER BY start_time;
    """, (MIN_MINUTES * 60, MAX_AGE_DAYS, SETTLE_MINUTES))
    return [load_activity(r["external_id"]) for r in rows]


def build_facts(a: dict) -> tuple[str, str]:
    """(facts block shown on Strava, extra context for the comment)."""
    kind = classify(a["type"], a["power_zones"], a["hr_zones"])
    day = a["start_time"].date()
    label = KIND_LABELS.get(kind) or re.sub(r"(?<=[a-z])(?=[A-Z])", " ", a["type"])  # "AlpineSki" -> "Alpine Ski"
    if kind == "endurance" and a["intensity_factor"] is not None and a["intensity_factor"] < RECOVERY_IF:
        label = "Recovery"
    first = [f"🎯 {label}"]
    intervals = [s for s in (a["interval_summary"] or []) if s]
    if intervals:
        first.append(re.sub(r"(\d)w\b", r"\1 W", " + ".join(intervals[:2])))  # Intervals.icu writes "261w"
    lines = [" · ".join(first)]

    hours = (a["moving_time_sec"] or 0) / 3600
    minutes = round((a["moving_time_sec"] or 0) / 60)
    stats = [f"{minutes // 60}h{minutes % 60:02d}" if minutes >= 60 else f"{minutes} min"]
    if a["distance_m"]:
        stats.append(f"{a['distance_m'] / 1000:.1f} km")
    if a["type"] in RIDE_TYPES and a["normalized_power_w"]:
        stats.append(f"NP {a['normalized_power_w']:.0f} W")
        if a["intensity_factor"]:
            stats.append(f"IF {a['intensity_factor']:.2f}")
    elif a["type"] in RUN_TYPES and a["distance_m"] and a["moving_time_sec"]:
        stats.append(_pace(a["moving_time_sec"] / (a["distance_m"] / 1000)))
    if a["training_load"] is not None:
        stats.append(f"TSS {a['training_load']:.0f}")
    if a["joules"]:
        stats.append(f"{_fmt_int(float(a['joules']) / 1000)} kJ")
    if a["temp_c"]:
        stats.append(f"{float(a['temp_c']):.0f} °C")
    lines.append("⚡ " + " · ".join(stats))

    ctl = query("SELECT day, ctl FROM daily_metrics WHERE day IN (%s, %s) AND ctl IS NOT NULL;",
                (day - timedelta(days=1), day))
    by_day = {r["day"]: r["ctl"] for r in ctl}
    if day in by_day and (day - timedelta(days=1)) in by_day:
        lines.append(f"📈 Fitness {by_day[day - timedelta(days=1)]:.1f} → {by_day[day]:.1f}")

    for ach in (a["achievements"] or [])[:2] if isinstance(a["achievements"], list) else []:
        if ach.get("message"):
            lines.append(f"🏆 {ach['message']}")

    context = [f"Activity: {a['type']} '{a['name']}' on {day:%A %Y-%m-%d}."]
    if gym := gym_focus(a["type"], a["name"]):
        context.append(f"Gym session: {gym}.")
    if a["decoupling_pct"] is not None and hours >= 1:
        d = a["decoupling_pct"]
        context.append(f"HR drift (decoupling) {d:.1f}%: " + (
            "likely HR sensor glitch" if d < -5 else "good aerobic durability" if d <= 5
            else "notable - heat, fatigue or fuelling"))
    context.append(f"Training focus: {get_focus()['short']}.")
    return "\n".join(lines), "\n".join(context)


def _readiness() -> str:
    try:
        r = current_readiness()
        return f"{r['level'].upper()} - {r['verdict']}"
    except Exception as e:
        logger.warning(f"Readiness unavailable: {e}")
        return "unknown"


def write_comment(facts: str, context: str, notes: str | None = None, next_session: str | None = None) -> str | None:
    try:
        llm = gemini_llm("strava_note_writer", temperature=0.3)
        extra = ATHLETE_NOTES.format(notes=notes.strip()) if notes and notes.strip() else ""
        private = PRIVATE.format(readiness=_readiness(), decision=(
            f"\nCoach's decided next session: {next_session}" if next_session else ""))
        content = llm.invoke(COMMENT_PROMPT.format(
            facts=facts, context=context, athlete_notes=extra, private=private,
            next_rule=NEXT_RULE if next_session else NO_NEXT_RULE)).content
        if isinstance(content, list):
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        return " ".join(str(content).split()) or None
    except Exception as e:
        logger.warning(f"Gemini comment failed, posting facts only: {e}")
        return None


def build_note(a: dict, notes: str | None = None) -> str:
    """`notes`: the athlete's extra context for the comment (falls back to what they gave last time)."""
    with trigger("strava_note"):
        facts, context = build_facts(a)
        decision = get_or_decide(a)
        comment = write_comment(facts, context, notes if notes is not None else a.get("strava_note_context"),
                                session_line(decision) if decision else None)
    return "\n".join([facts, "", comment, SIGNATURE] if comment else [facts, SIGNATURE])


def merge_description(current: str | None, previous_note: str | None, note: str) -> str:
    """The athlete's own text, then our note. A previous note of ours is replaced, nothing else."""
    text = (current or "").replace("\r\n", "\n")
    if previous_note and previous_note in text:
        text = text.replace(previous_note, "")
    text = text.strip()
    return f"{text}\n\n{note}" if text else note


def find_strava_id(a: dict, token: str) -> str | None:
    if a["strava_id"]:
        return a["strava_id"]
    start = a["start_time"].astimezone(timezone.utc)
    candidates = strava.fetch_activities(token, after=start - timedelta(hours=1))
    best = min(candidates, key=lambda s: abs(strava.utc_ts(s["start_date"]) - start.timestamp()), default=None)
    if best and abs(strava.utc_ts(best["start_date"]) - start.timestamp()) <= MATCH_WINDOW_SEC:
        _execute("UPDATE activities SET strava_id = %s WHERE id = %s;", (str(best["id"]), a["id"]))
        return str(best["id"])
    return None


def publish_note(a: dict, note: str, token: str, notes: str | None = None) -> str:
    """Writes `note` (as previewed) into the activity's Strava description. Returns the Strava id.
    Used for manual (re)posts, which override #nocoach - the athlete asked for it explicitly."""
    strava_id = find_strava_id(a, token)
    if not strava_id:
        raise RuntimeError("This activity isn't on Strava yet (or couldn't be matched by start time).")
    current = strava.get_activity(token, strava_id).get("description")
    strava.update_activity(token, strava_id, description=merge_description(current, a["strava_note"], note))
    _execute("UPDATE activities SET strava_note = %s, strava_note_at = now(), strava_note_context = %s WHERE id = %s;",
             (note, (notes or None) if notes is not None else a.get("strava_note_context"), a["id"]))
    return strava_id


def post_note(a: dict, token: str, dry_run: bool = False) -> str:
    """Automatic posting (timer): builds and posts the note, honouring #nocoach. Returns a one-line status."""
    strava_id = find_strava_id(a, token)
    if not strava_id:
        return f"{a['external_id']}: not on Strava yet - will retry"
    current = strava.get_activity(token, strava_id).get("description")
    if current and OPT_OUT_TAG in current:
        if not dry_run:
            _execute("UPDATE activities SET strava_note_at = now() WHERE id = %s;", (a["id"],))
        return f"{a['external_id']}: {OPT_OUT_TAG} - skipped"

    note = build_note(a)
    if dry_run:
        return (f"{a['external_id']} -> Strava {strava_id} (dry run). New description:\n"
                f"{merge_description(current, a['strava_note'], note)}")
    publish_note(a, note, token)
    return f"{a['external_id']} -> Strava {strava_id}: note posted"


def strava_url(strava_id: str | None) -> str | None:
    return f"https://www.strava.com/activities/{strava_id}" if strava_id else None


def find_noteable(on_day: date | None = None) -> dict | None:
    """The latest activity (any type or length), optionally on a given day - the default target of a manual push."""
    rows = query(f"""
        SELECT external_id FROM activities
        WHERE superseded_by IS NULL {"AND start_time::date = %s" if on_day else ""}
        ORDER BY start_time DESC LIMIT 1;
    """, (on_day,) if on_day else ())
    return load_activity(rows[0]["external_id"]) if rows else None


def require_write_scope():
    if "activity:write" not in strava.granted_scopes():
        raise RuntimeError("Strava token lacks activity:write - re-run ingest/strava_auth.py.")


def post_pending(dry_run: bool = False) -> list[str]:
    acts = [a for a in pending_activities() if a]
    if not acts:
        return []
    if not dry_run:
        require_write_scope()
    token = strava.access_token()
    results = []
    for a in acts:
        try:
            results.append(post_note(a, token, dry_run))
        except Exception as e:
            logger.error(f"Strava note for {a['external_id']} failed: {e}", exc_info=True)
            results.append(f"{a['external_id']}: failed - {e}")
    return results

