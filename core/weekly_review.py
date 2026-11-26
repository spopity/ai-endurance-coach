"""The weekly review: once a week the AI steps back from the day-to-day decisions and asks whether
the plan itself still fits the athlete, and proposes 0-3 changes the athlete approves one by one.

- **Facts from code** (`facts()`): the coach's full context (graph.global_preprocessor: readiness,
  week_ahead, plan, progress, progression, push, goals...) plus the week just finished: the plan's
  target and verdict, every session with its load and the athlete's RPE/legs, the morning check-ins,
  the morning vs 13:00 readiness log, the comeback ramp and the previous review's decisions.
- **Judgment from the AI** (Claude, Gemini as fallback): a short summary and proposals, each one of
  a fixed menu of ACTIONS that existing code applies. Nothing the AI writes changes anything by itself.
- **The athlete decides:** Telegram shows each proposal with Apply / Discard. Apply runs the action
  and logs it in the adjustment log (core/adjustments.py), so the quarterly review judges whether it
  helped. Proposals expire when the next review is written.

State: state/weekly_reviews/<date>.json.
"""
import json
import logging
import os
import re
from datetime import date, datetime, timedelta

from core.overview import query

logger = logging.getLogger("AI_COACH_V1.weekly_review")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW_DIR = os.path.join(ROOT, "state", "weekly_reviews")
GUIDANCE_KEY = "weekly_guidance"
MAX_PROPOSALS = 3
GUIDANCE_DAYS = 7
MAX_TEXT = 400

ACTIONS = {
    "guidance": "a note the daily coach follows for the next 7 days (e.g. 'long rides strictly Zone 2: heart-rate "
                "drift is still 7%'). Params: {\"text\": str}. It never overrides readiness, illness or injury rules.",
    "comeback_ramp": "change the planned fitness (CTL) ramp while CTL is under 50, from next week on. "
                     "Params: {\"ramp\": 4 | 5 | 6} (CTL/week).",
    "push": "start a push experiment above the plan (volume, intensity or both) - the existing push rules and "
            "blocks apply. Params: {\"kind\": \"volume\" | \"intensity\" | \"both\", \"days\": 3-14}.",
    "pin": "put a specific session on a specific day of the next 7 days. "
           "Params: {\"date\": \"YYYY-MM-DD\", \"session\": str}.",
}

SYSTEM_PROMPT = """You are the head coach doing the WEEKLY REVIEW of a self-coached endurance athlete's plan.
The day-to-day decisions are made elsewhere; your job is to step back once a week and judge whether the
plan still fits the athlete, using the facts below, and propose at most {max_proposals} changes the athlete
will approve or reject one by one.

How to judge, as an experienced endurance coach would:
- Did the athlete absorb last week? Compare the load done with the target, and look at how sessions felt
  (RPE, legs), the check-ins, HRV against its normal, readiness through the week, and form (TSB).
- Is the training working? Use `progress` (4-week markers and diagnosis) and `progression` levels; one week is
  noise, so look for trends over 2-4 weeks before proposing a change.
- Consistency beats heroics: if sessions were missed, the fix is usually making the week doable, not harder.
- Respect the athlete's choices: they want to be pushed above the plan when they can take it (push
  experiments), they keep the dual-sport block (max 3 runs a week), they declined a cap on the CTL ramp, and
  the readiness rules were relaxed on purpose (single signals trim a session instead of cancelling it).
  Don't propose tightening those.
- Numbers come from the facts: quote them, never invent or recompute them.
- No change is a valid outcome. Propose only what the facts justify; say why in one or two sentences with
  the numbers.

Allowed actions (anything else is ignored):
{actions}

Answer with ONLY a JSON object, no prose around it:
{{"summary": "3-5 plain sentences on last week and where the athlete stands",
  "proposals": [{{"action": "<one of the actions>", "params": {{...}}, "title": "short, plain",
                 "why": "1-2 sentences with the numbers"}}]}}

Today is {today}. The week reviewed is {week_from} to {week_to}.

FACTS:
{facts}
"""


# --- facts ---------------------------------------------------------------------------------------

def _week(today: date) -> tuple[date, date]:
    """The last full Monday-Sunday week before today (on a Sunday evening: the week ending today)."""
    end = today if today.weekday() == 6 else today - timedelta(days=today.weekday() + 1)
    return end - timedelta(days=6), end


def _rows(sql: str, params=()) -> list[dict]:
    try:
        return query(sql, params)
    except Exception as e:
        logger.warning(f"Weekly review: query failed ({e})")
        return []


def last_week(today: date) -> dict:
    start, end = _week(today)
    block = _rows("SELECT phase, target_weekly_load_min, target_weekly_load_max, is_deload, status, notes "
                  "FROM training_blocks WHERE start_date <= %s AND end_date >= %s ORDER BY start_date DESC LIMIT 1;",
                  (start, start))
    sessions = _rows("""
        SELECT a.start_time::date AS day, a.name, a.type, round(a.moving_time_sec / 60.0) AS minutes,
               round(a.training_load) AS load, f.rpe, f.legs
        FROM activities a LEFT JOIN session_feedback f ON f.external_id = a.external_id
        WHERE a.superseded_by IS NULL AND a.start_time::date BETWEEN %s AND %s ORDER BY a.start_time;""",
                     (start, end))
    checkins = _rows("SELECT day, body, sleep, stress, motivation FROM daily_checkin "
                     "WHERE day BETWEEN %s AND %s AND answered_at IS NOT NULL ORDER BY day;", (start, end))
    from core.readiness import MIDDAY_LOG
    try:
        with open(MIDDAY_LOG) as f:
            log = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        log = []
    readiness = [{"date": e["date"], "morning": (e.get("morning") or {}).get("level"),
                  "midday": e["midday"]["level"], "tsb": e["midday"].get("tsb")}
                 for e in log if start.isoformat() <= e["date"] <= end.isoformat()]
    return {"from": start.isoformat(), "to": end.isoformat(),
            "plan": block[0] if block else None,
            "load_done": round(sum(float(s["load"] or 0) for s in sessions)),
            "sessions": sessions, "checkins": checkins, "readiness_by_day": readiness}


def facts(today: date) -> dict:
    from graph import global_preprocessor
    from core import periodization
    ctx = global_preprocessor({})["metrics_context"]
    prev = previous(today)
    return {
        "last_week": last_week(today),
        "comeback_ramp": {"now": periodization.comeback_ramp(periodization.monday(today + timedelta(days=1))),
                          "choices": list(periodization.RAMP_CHOICES),
                          "applies_while_ctl_under": periodization.COMEBACK_CTL},
        "guidance_now": guidance(today),
        "previous_review": prev and {"date": prev["date"], "proposals": [
            {k: p.get(k) for k in ("action", "params", "title", "status")} for p in prev["proposals"]]},
        "coach_context": ctx,
    }


# --- the AI --------------------------------------------------------------------------------------

def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in the review")
    return json.loads(m.group(0))


def _validate(p: dict, today: date) -> str | None:
    """Why a proposal can't be offered, or None when it can."""
    from core import availability, periodization, push
    a, params = p.get("action"), p.get("params") or {}
    if a not in ACTIONS:
        return f"unknown action {a!r}"
    if a == "guidance":
        t = str(params.get("text") or "").strip()
        return None if 0 < len(t) <= MAX_TEXT else "guidance text missing or too long"
    if a == "comeback_ramp":
        try:
            r = float(params.get("ramp"))
        except (TypeError, ValueError):
            return "ramp is not a number"
        if r not in periodization.RAMP_CHOICES:
            return f"ramp must be one of {periodization.RAMP_CHOICES}"
        now = periodization.comeback_ramp(periodization.monday(today + timedelta(days=1)))
        return None if r != now else f"the ramp is already {now:g}"
    if a == "push":
        days = params.get("days", push.DEFAULT_DAYS)
        ok = params.get("kind") in push.KINDS and isinstance(days, int) and 3 <= days <= push.MAX_DAYS
        return None if ok else "push needs kind volume/intensity/both and 3-14 days"
    if a == "pin":
        try:
            d = date.fromisoformat(str(params.get("date")))
        except ValueError:
            return "pin date is not a date"
        if not today < d <= today + timedelta(days=7):
            return "pin date must be in the next 7 days"
        if availability.day_status(d) and availability.day_status(d).get("kind") in availability.NO_TRAINING:
            return "pin date is a no-training day"
        s = str(params.get("session") or "").strip()
        return None if 0 < len(s) <= 200 else "pin session missing or too long"
    return None


def ask(fx: dict, today: date) -> dict:
    from core.claude_agent import claude_or_gemini
    from core.llm import gemini_llm
    start, end = fx["last_week"]["from"], fx["last_week"]["to"]
    prompt = SYSTEM_PROMPT.format(
        max_proposals=MAX_PROPOSALS, today=today.isoformat(), week_from=start, week_to=end,
        actions="\n".join(f"- {k}: {v}" for k, v in ACTIONS.items()),
        facts=json.dumps(fx, indent=1, default=str))
    question = "Write the weekly review now, as the JSON object."

    def gemini() -> str:
        from langchain_core.messages import HumanMessage, SystemMessage
        out = gemini_llm("weekly_review", temperature=0.2).invoke([SystemMessage(content=prompt), HumanMessage(content=question)])
        return out.content if isinstance(out.content, str) else json.dumps(out.content)

    return _parse(claude_or_gemini("weekly_review", prompt, [], [], question, gemini))


# --- storage -------------------------------------------------------------------------------------

def _path(d: str) -> str:
    return os.path.join(REVIEW_DIR, f"{d}.json")


def _save(review: dict):
    os.makedirs(REVIEW_DIR, exist_ok=True)
    tmp = _path(review["date"]) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(review, f, indent=1, default=str)
    os.replace(tmp, _path(review["date"]))


def load(d: str) -> dict | None:
    try:
        with open(_path(d)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def previous(today: date) -> dict | None:
    """The latest review written before today."""
    try:
        names = sorted(n[:-5] for n in os.listdir(REVIEW_DIR) if n.endswith(".json") and n[:-5] < today.isoformat())
    except OSError:
        return None
    return load(names[-1]) if names else None


def run(today: date | None = None) -> dict:
    """Writes this week's review (replacing one already written today) and returns it."""
    from core.schedule import TZ
    today = today or datetime.now(TZ).date()
    fx = facts(today)
    raw = ask(fx, today)
    proposals = []
    for p in raw.get("proposals") or []:
        if len(proposals) == MAX_PROPOSALS:
            break
        problem = _validate(p, today)
        if problem:
            logger.info(f"Weekly review: proposal dropped ({problem}): {p}")
            continue
        proposals.append({"id": len(proposals), "action": p["action"], "params": p.get("params") or {},
                          "title": str(p.get("title") or p["action"])[:120], "why": str(p.get("why") or "")[:MAX_TEXT],
                          "status": "pending"})
    prev = previous(today)
    if prev:   # a new review replaces the last one's open proposals
        for p in prev["proposals"]:
            if p["status"] == "pending":
                p["status"] = "expired"
        _save(prev)
    review = {"date": today.isoformat(), "week": [fx["last_week"]["from"], fx["last_week"]["to"]],
              "summary": str(raw.get("summary") or "").strip(), "proposals": proposals,
              "at": datetime.now(TZ).isoformat(timespec="minutes")}
    _save(review)
    logger.info(f"Weekly review {today}: {len(proposals)} proposal(s).")
    return review


# --- applying ------------------------------------------------------------------------------------

def guidance(today: date | None = None) -> dict | None:
    """The coach's note from an applied review, while it lasts: {text, until}."""
    rows = _rows("SELECT value FROM athlete_settings WHERE key = %s;", (GUIDANCE_KEY,))
    g = (rows[0]["value"] if rows else None) or None
    today = today or date.today()
    return g if g and g.get("until", "") >= today.isoformat() else None


def _set_guidance(text: str, today: date):
    from core.db import get_connection
    value = {"text": text, "from": today.isoformat(), "until": (today + timedelta(days=GUIDANCE_DAYS)).isoformat()}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO athlete_settings (key, value, updated_at) VALUES (%s, %s, now())
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();""",
                        (GUIDANCE_KEY, json.dumps(value)))
        conn.commit()
    finally:
        conn.close()


def resolve(review_date: str, pid: int, apply: bool, today: date | None = None) -> str:
    """Applies or discards one proposal. Returns a line for the athlete."""
    from core import adjustments, availability, periodization, push
    from core.schedule import TZ
    today = today or datetime.now(TZ).date()
    review = load(review_date)
    if not review or not 0 <= pid < len(review["proposals"]):
        return "That proposal no longer exists."
    p = review["proposals"][pid]
    if p["status"] != "pending":
        return f"Already {p['status']}."
    if not apply:
        p["status"] = "discarded"
        _save(review)
        return f"Discarded: {p['title']}"
    problem = _validate(p, today)
    if problem:
        return f"Can't apply it any more: {problem}."
    a, params = p["action"], p["params"]
    try:
        if a == "guidance":
            _set_guidance(params["text"].strip(), today)
            done = f"the coach follows this until {(today + timedelta(days=GUIDANCE_DAYS)):%a %d %b}"
        elif a == "comeback_ramp":
            nxt = periodization.monday(today) + timedelta(days=7)
            periodization.set_comeback_ramp(float(params["ramp"]), nxt)
            done = f"+{float(params['ramp']):g} CTL/week from {nxt:%a %d %b} (the plan recalculates tonight)"
        elif a == "push":
            st = push.start(params["kind"], int(params.get("days", push.DEFAULT_DAYS)), note="weekly review",
                            today=today)
            done = f"push running until {st.get('until') or 'its end'}"
        else:   # pin
            d = date.fromisoformat(params["date"])
            availability.add("planned", d, 1, note="weekly review", session=params["session"].strip())
            done = f"pinned on {d:%a %d %b}"
    except Exception as e:
        logger.warning(f"Weekly review proposal {review_date}/{pid} failed: {e}", exc_info=True)
        return f"Couldn't apply it: {e}"
    p["status"] = "applied"
    p["applied_at"] = datetime.now(TZ).isoformat(timespec="minutes")
    _save(review)
    adjustments.log("review", p["title"], p["why"], today, action=a, params=params)
    if a in ("pin", "push", "guidance"):
        try:
            from core.next_session import replan
            replan()
        except Exception as e:
            logger.warning(f"Re-plan after the weekly review failed: {e}")
    return f"Applied: {p['title']} - {done}."


def markdown(review: dict) -> str:
    lines = [f"🗓 **Weekly review** ({review['week'][0]} to {review['week'][1]})", "", review["summary"] or "-"]
    if not review["proposals"]:
        lines += ["", "No changes proposed: the plan fits as it is."]
    return "\n".join(lines)


def proposal_text(p: dict) -> str:
    return f"**{p['id'] + 1}. {p['title']}**\n{p['why']}"
