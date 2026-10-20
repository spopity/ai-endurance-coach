"""
Power pacing / fade for past activities, from the second-by-second watts stream on
Intervals.icu (streams aren't stored locally - only per-activity summaries are). Answers
"did my power drop before the end?", which decoupling_pct can't: that is HR drift vs power.

The recording is split into quarters by recorded time and each quarter gets normalized
power (NP, 30 s rolling) - NP, not average power, so coasting on descents or in a bunch
doesn't read as fading. Leading/trailing zero-power stretches (sitting at the start line,
standing after the finish) are trimmed first.
"""
import logging
from functools import lru_cache

import requests

from core.db import get_connection, load_env

logger = logging.getLogger("AI_COACH_V1.pacing")

BASE_URL = "https://intervals.icu"
MAX_ACTIVITIES_PER_CALL = 10
# Last quarter NP vs the strongest earlier quarter: inside this band = held, below = faded.
FADE_PCT = 5.0


def _np(watts: list[float]) -> float | None:
    if len(watts) < 30:
        return None
    rolling, window = [], sum(watts[:30])
    rolling.append(window / 30)
    for i in range(30, len(watts)):
        window += watts[i] - watts[i - 30]
        rolling.append(window / 30)
    return (sum(p ** 4 for p in rolling) / len(rolling)) ** 0.25


@lru_cache(maxsize=256)
def _watts_stream(external_id: str) -> tuple[float, ...]:
    env = load_env()
    resp = requests.get(f"{BASE_URL}/api/v1/activity/{external_id}/streams.json", params={"types": "watts"},
                        auth=("API_KEY", env["INTERVALS_API_KEY"]), timeout=60)
    resp.raise_for_status()
    stream = next((s for s in resp.json() if s.get("type") == "watts"), None)
    return tuple(float(w or 0) for w in (stream or {}).get("data") or [])


def _pacing_one(external_id: str, meta: dict | None) -> dict:
    out = {"external_id": external_id}
    if meta:
        out.update(date=meta["start_time"].date().isoformat(), name=meta["name"], type=meta["type"])
    try:
        watts = list(_watts_stream(external_id))
    except Exception as e:
        logger.warning(f"pacing: stream fetch failed for {external_id}: {e}")
        return {**out, "error": f"could not fetch the power stream ({e})"}

    start = next((i for i, w in enumerate(watts) if w > 0), len(watts))
    end = len(watts) - next((i for i, w in enumerate(reversed(watts)) if w > 0), len(watts))
    watts = watts[start:end]
    if len(watts) < 600 or not any(watts):
        return {**out, "error": "no usable power data (no power meter or under 10 minutes)"}

    q = len(watts) // 4
    quarters = [watts[i * q:(i + 1) * q] if i < 3 else watts[3 * q:] for i in range(4)]
    np_quarters = [_np(part) for part in quarters]
    np_first_three = _np(watts[:3 * q])
    np_last_10pct = _np(watts[int(len(watts) * 0.9):])
    # The verdict compares the last quarter with the strongest earlier one, not the average of
    # the first three: recordings often include a warm-up or the ride to the start, which would
    # drag that average down and hide a real fade.
    best_earlier = max(np_quarters[:3])
    vs_best = round((np_quarters[3] / best_earlier - 1) * 100, 1)
    verdict = "faded" if vs_best <= -FADE_PCT else "finished stronger" if vs_best >= FADE_PCT else "held"
    result = {
        **out,
        "recorded_minutes": round(len(watts) / 60),
        "np_whole": round(_np(watts)),
        "np_by_quarter": [round(p) for p in np_quarters],
        "avg_power_by_quarter": [round(sum(part) / len(part)) for part in quarters],
        "np_last_10pct": round(np_last_10pct) if np_last_10pct else None,
        "last_quarter_vs_best_earlier_quarter_pct": vs_best,
        "last_quarter_vs_first_three_pct": round((np_quarters[3] / np_first_three - 1) * 100, 1),
        "verdict": verdict,
    }
    if np_quarters[0] < 0.8 * max(np_quarters[1:]):
        result["note"] = "first quarter much easier than the rest - recording likely includes a warm-up/ride to the start"
    return result


def analyze_power_pacing(external_ids: list[str]) -> list[dict]:
    """Quarter-by-quarter NP and a held/faded verdict for up to 10 activities."""
    ids = list(dict.fromkeys(external_ids))[:MAX_ACTIVITIES_PER_CALL]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT external_id, start_time, name, type FROM activities WHERE external_id = ANY(%s);", (ids,))
            meta = {r["external_id"]: r for r in cur.fetchall()}
    finally:
        conn.close()
    results = [_pacing_one(i, meta.get(i)) for i in ids]
    if len(external_ids) > MAX_ACTIVITIES_PER_CALL:
        results.append({"note": f"Only the first {MAX_ACTIVITIES_PER_CALL} ids were analysed - call again for the rest."})
    return results
