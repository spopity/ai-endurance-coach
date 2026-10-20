"""
JSON API behind the dashboard (web/). Everything lives under /api so it can be
put behind authentication in one place when the dashboard is exposed outside
the home network.
"""
import asyncio
import json
import logging
import secrets
import time
from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.chat import SYNC_WINDOW_DAYS, run_sync, stream_reply
from core.overview import build_overview, cached as _cached, clear_cache, query as _query, rnd as _f
from core.next_session import (
    ProposalError, apply_proposal, discard_proposal, get_or_decide, latest_activity, latest_decision, session_line,
    why_line,
)
from core.readiness import current_readiness
from core.focus import MODES as FOCUS_MODES, get_focus, set_focus
from core.injury_checks import run_injury_checks
from core.plan import upcoming_plan
from core import strava
from core.strava_notes import build_note, load_activity, publish_note, require_write_scope, strava_url
from core.races import fetch_upcoming_races
from core.usage import usage_summary

logger = logging.getLogger("AI_COACH_V1.dashboard_api")
router = APIRouter(prefix="/api")

SPORT_GROUPS = {
    "Ride": "Bike", "GravelRide": "Bike", "MountainBikeRide": "Bike", "VirtualRide": "Bike", "EBikeRide": "Bike",
    "Run": "Run", "TrailRun": "Run", "VirtualRun": "Run",
    "WeightTraining": "Strength", "Workout": "Strength", "Yoga": "Strength",
    "Swim": "Swim", "OpenWaterSwim": "Swim",
}

@router.get("/overview")
def overview():
    return build_overview()


@router.get("/pmc")
def pmc(days: int = 180):
    days = max(14, min(days, 6000))
    since = date.today() - timedelta(days=days)
    rows = _query("SELECT day, ctl, atl FROM daily_metrics WHERE day >= %s AND ctl IS NOT NULL ORDER BY day;", (since,))
    loads = _query("""
        SELECT start_time::date AS day, SUM(training_load) AS load FROM activities
        WHERE superseded_by IS NULL AND start_time::date >= %s GROUP BY 1;
    """, (since,))
    load_by_day = {r["day"]: float(r["load"] or 0) for r in loads}
    return [{"day": r["day"].isoformat(), "ctl": _f(r["ctl"]), "atl": _f(r["atl"]),
             "tsb": _f(float(r["ctl"]) - float(r["atl"])), "load": _f(load_by_day.get(r["day"], 0), 0)}
            for r in rows]


@router.get("/wellness")
def wellness(days: int = 90):
    days = max(7, min(days, 3650))
    rows = _query("""
        SELECT day, hrv, resting_hr, sleep_duration_sec, sleep_score, weight_kg FROM daily_metrics
        WHERE day >= %s ORDER BY day;
    """, (date.today() - timedelta(days=days),))
    return [{"day": r["day"].isoformat(), "hrv": _f(r["hrv"]), "resting_hr": _f(r["resting_hr"], 0),
             "sleep_h": _f(float(r["sleep_duration_sec"]) / 3600) if r["sleep_duration_sec"] else None,
             "sleep_score": _f(r["sleep_score"], 0), "weight_kg": _f(r["weight_kg"])} for r in rows]


@router.get("/weekly")
def weekly(weeks: int = 12):
    weeks = max(4, min(weeks, 104))
    first_monday = date.today() - timedelta(days=date.today().weekday()) - timedelta(weeks=weeks - 1)
    acts = _query("""
        SELECT start_time::date AS day, type, training_load, moving_time_sec FROM activities
        WHERE superseded_by IS NULL AND start_time::date >= %s;
    """, (first_monday,))
    blocks = _query("""
        SELECT start_date, end_date, phase, target_weekly_load_min, target_weekly_load_max, is_deload, status
        FROM training_blocks WHERE end_date >= %s ORDER BY start_date;
    """, (first_monday,))
    out = []
    for i in range(weeks):
        start = first_monday + timedelta(weeks=i)
        end = start + timedelta(days=6)
        by_sport = defaultdict(float)
        hours = 0.0
        for a in acts:
            if start <= a["day"] <= end:
                by_sport[SPORT_GROUPS.get(a["type"], "Other")] += float(a["training_load"] or 0)
                hours += (a["moving_time_sec"] or 0) / 3600
        block = next((b for b in blocks if b["start_date"] <= start + timedelta(days=3) <= b["end_date"]), None)
        out.append({
            "week": start.isoformat(), "load": round(sum(by_sport.values())), "hours": round(hours, 1),
            "by_sport": {k: round(v) for k, v in by_sport.items()},
            "target_min": _f(block["target_weekly_load_min"], 0) if block else None,
            "target_max": _f(block["target_weekly_load_max"], 0) if block else None,
            "phase": block["phase"] + (" (deload)" if block["is_deload"] and block["phase"] != "Recovery" else "") if block else None,
            "status": block["status"] if block else None,
        })
    return out


@router.get("/plan")
def plan(weeks: int = 8):
    return upcoming_plan(max(1, min(weeks, 26)))


def _next_session_payload() -> dict:
    d = latest_decision()
    try:
        readiness = current_readiness()
    except Exception as e:
        logger.warning(f"Readiness unavailable: {e}")
        readiness = None
    return {
        "readiness": {"level": readiness["level"], "verdict": readiness["verdict"]} if readiness else None,
        "decision": {**d, "session": session_line(d["decision"]), "why": why_line(d["decision"])} if d else None,
    }


class NextSessionRequest(BaseModel):
    redo: bool = False


@router.get("/next-session")
def next_session():
    return _next_session_payload()


@router.post("/next-session")
async def next_session_decide(req: NextSessionRequest):
    a = await asyncio.to_thread(latest_activity)
    if not a:
        raise HTTPException(status_code=404, detail="No activities yet.")
    decision = await asyncio.to_thread(get_or_decide, a, False, req.redo)
    if not decision:
        raise HTTPException(status_code=502, detail="The coach couldn't be reached - try again.")
    logger.info(f"Next session {'re-' if req.redo else ''}decided from the dashboard.")
    return await asyncio.to_thread(_next_session_payload)


@router.post("/next-session/proposals/{pid}/{action}")
async def next_session_proposal(pid: int, action: str):
    """Apply or discard a next-session change the coach proposed in chat - the only way a chat
    changes the shared decision, and only with the athlete's click."""
    if action not in ("apply", "discard"):
        raise HTTPException(status_code=404, detail="Unknown action.")
    try:
        await asyncio.to_thread(apply_proposal if action == "apply" else discard_proposal, pid)
    except ProposalError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info(f"Next-session proposal #{pid} {action} from the dashboard.")
    return await asyncio.to_thread(_next_session_payload)


class FocusRequest(BaseModel):
    mode: str


@router.get("/focus")
def focus():
    f = get_focus()
    return {"mode": f["mode"], "since": f["since"], "options": {k: v["label"] for k, v in FOCUS_MODES.items()}}


@router.post("/focus")
def focus_set(req: FocusRequest):
    if req.mode not in FOCUS_MODES:
        raise HTTPException(status_code=400, detail=f"Unknown training focus {req.mode!r}")
    set_focus(req.mode)
    logger.info(f"Training focus set to {req.mode} from the dashboard.")
    clear_cache()
    return focus()


@router.get("/activities")
def activities(limit: int = 25):
    rows = _query("""
        SELECT external_id, start_time, type, name, distance_m, moving_time_sec, total_elevation_gain_m,
               avg_power_w, normalized_power_w, intensity_factor, training_load, avg_hr, decoupling_pct,
               efficiency_factor, avg_cadence, race, feel, strava_id, strava_note_at
        FROM activities WHERE superseded_by IS NULL ORDER BY start_time DESC LIMIT %s;
    """, (max(1, min(limit, 200)),))
    return [{
        "id": r["external_id"], "start": r["start_time"].isoformat(), "type": r["type"],
        "group": SPORT_GROUPS.get(r["type"], "Other"), "name": r["name"],
        "distance_km": _f((r["distance_m"] or 0) / 1000) if r["distance_m"] else None,
        "hours": _f((r["moving_time_sec"] or 0) / 3600, 2) if r["moving_time_sec"] else None,
        "elevation_m": _f(r["total_elevation_gain_m"], 0), "avg_power": _f(r["avg_power_w"], 0),
        "np": _f(r["normalized_power_w"], 0), "if": _f(r["intensity_factor"], 2), "load": _f(r["training_load"], 0),
        "avg_hr": _f(r["avg_hr"], 0), "decoupling": _f(r["decoupling_pct"]), "ef": _f(r["efficiency_factor"], 2),
        "cadence": _f(r["avg_cadence"], 0), "race": bool(r["race"]), "feel": r["feel"],
        "strava_note_ok": True, "strava_url": strava_url(r["strava_id"]),
        "strava_note_at": r["strava_note_at"].isoformat() if r["strava_note_at"] else None,
    } for r in rows]


# --- Strava coach notes (preview, then post exactly what was previewed) ---

PREVIEW_TTL_SECONDS = 30 * 60
_previews: dict[str, dict] = {}


def _noteable(external_id: str) -> dict:
    a = load_activity(external_id)
    if not a:
        raise HTTPException(status_code=404, detail="No activity with that id.")
    return a


class StravaNoteRequest(BaseModel):
    context: str | None = None


class StravaPostRequest(BaseModel):
    preview_id: str


@router.get("/strava-note/{external_id}")
def strava_note(external_id: str):
    a = _noteable(external_id)
    return {"note": a["strava_note"], "context": a["strava_note_context"], "strava_url": strava_url(a["strava_id"]),
            "posted_at": a["strava_note_at"].isoformat() if a["strava_note_at"] else None}


@router.post("/strava-note/{external_id}/preview")
async def strava_note_preview(external_id: str, req: StravaNoteRequest):
    a = _noteable(external_id)
    context = (req.context or "").strip() or None
    note = await asyncio.to_thread(build_note, a, context if context is not None else "")
    now = time.time()
    for k in [k for k, v in _previews.items() if now - v["at"] > PREVIEW_TTL_SECONDS]:
        del _previews[k]
    preview_id = secrets.token_urlsafe(12)
    _previews[preview_id] = {"external_id": external_id, "note": note, "context": context, "at": now}
    return {"preview_id": preview_id, "note": note}


@router.post("/strava-note/{external_id}")
async def strava_note_post(external_id: str, req: StravaPostRequest):
    preview = _previews.get(req.preview_id)
    if not preview or preview["external_id"] != external_id:
        raise HTTPException(status_code=409, detail="Preview expired - please preview again.")
    a = _noteable(external_id)
    try:
        require_write_scope()
        token = await asyncio.to_thread(strava.access_token)
        strava_id = await asyncio.to_thread(publish_note, a, preview["note"], token, preview["context"] or "")
    except Exception as e:
        logger.error(f"Posting Strava note for {external_id} failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=str(e))
    _previews.pop(req.preview_id, None)
    logger.info(f"Strava note posted for {external_id} from the dashboard.")
    return {"strava_url": strava_url(strava_id), "posted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


@router.get("/injury")
def injury():
    return _cached("injury", 300, run_injury_checks)


@router.get("/races")
def races():
    try:
        return _cached("races", 600, fetch_upcoming_races)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Intervals.icu calendar unavailable: {e}")


@router.get("/usage")
def usage(days: int = 7):
    """System page: external calls per service, Gemini tokens and estimated cost (core/api_log.py)."""
    return usage_summary(days)


# --- actions ---

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    agent: str | None = None
    local_only: bool = False


@router.post("/chat")
async def chat(req: ChatRequest):
    logger.info(f"Dashboard chat: {req.message!r} ({len(req.history)} prior messages, local_only={req.local_only})")

    async def events():
        async for event in stream_reply(req.message, req.history, req.agent, req.local_only):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/sync")
async def sync():
    try:
        message = await run_sync(SYNC_WINDOW_DAYS)
    except Exception as e:
        logger.error(f"Sync failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    clear_cache()
    return {"message": message}
