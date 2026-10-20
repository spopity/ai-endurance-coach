"""
Training focus, switched from the dashboard: 'cycling' (default - peak for cycling,
maximise FTP, running is maintenance) or 'dual' (winter dual-sport - cycling and
running both primary). The stored training_blocks plan stays untouched; dual mode
is applied on top of it when the plan is read (session mix + run focus), so
switching back restores the cycling plan exactly.
"""
import json
from datetime import date

from core.db import get_connection

DEFAULT = "cycling"

MODES = {
    "cycling": {
        "label": "Cycling focus",
        "primary_goal": "Peak for cycling by maximizing FTP growth through properly periodized training.",
        "secondary_goal": "Maintain baseline running fitness and structural strength.",
        "running_rule": (
            "**Running Volume (Summer Constraints):** Running is strictly for maintenance. During the summer, "
            "limit running suggestions to a maximum of one 5km run per week. If a run has already been completed "
            "in the last 7 days, do not prescribe another."
        ),
        "short": "cycling first (maximise FTP); running is maintenance only",
    },
    "dual": {
        "label": "Dual-sport (bike + run)",
        "primary_goal": (
            "Dual-sport winter block: build cycling AND running fitness together, both sports are equally "
            "important. Keep FTP-supporting bike work (sweet spot/threshold) but at slightly lower bike volume."
        ),
        "secondary_goal": "Structural strength to support the higher running load (injury prevention).",
        "running_rule": (
            "**Running (Dual-Sport Winter Block):** Running is a primary sport now. Plan 3-4 runs a week: one "
            "quality run (tempo/threshold by heart rate, or hill repeats), one long easy run, the rest easy and "
            "conversational. Grow weekly run volume by at most ~10% a week. Never put a hard run the day after "
            "a hard ride or a leg gym session (title names legs; other gym sessions don't count), or vice versa - alternate hard bike and hard run days. "
            "In a deload week, runs are easy only."
        ),
        "short": "dual-sport: cycling and running are both primary goals this winter",
    },
}

RUN_FOCUS = {
    False: " Run: one quality session (tempo/threshold by HR or hill repeats), one long easy run, the rest easy.",
    True: " Run: easy runs only, long run shortened.",
}
# Early base (see core/periodization.py): no run intensity yet either.
RUN_FOCUS_BASE = " Run: easy and conversational, one long easy run, strides or short hill sprints at most."


def get_focus() -> dict:
    """{'mode': 'cycling'|'dual', 'since': iso date or None, **MODES[mode]}."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM athlete_settings WHERE key = 'training_focus';")
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        row = None
    value = (row or {}).get("value") or {}
    mode = value.get("mode") if value.get("mode") in MODES else DEFAULT
    return {"mode": mode, "since": value.get("since"), **MODES[mode]}


def set_focus(mode: str) -> dict:
    if mode not in MODES:
        raise ValueError(f"Unknown training focus {mode!r}")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO athlete_settings (key, value, updated_at) VALUES ('training_focus', %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
            """, (json.dumps({"mode": mode, "since": date.today().isoformat()}),))
        conn.commit()
    finally:
        conn.close()
    return get_focus()


def adjust_sessions(sessions: dict | None, is_deload: bool, mode: str) -> dict | None:
    """Dual mode: one more run in build weeks, one endurance ride and one gym session fewer."""
    if not sessions or mode != "dual":
        return sessions
    s = dict(sessions)
    s["run"] = max(int(s.get("run", 0)), 3 if is_deload else 4)
    s["endurance"] = max(1, int(s.get("endurance", 0)) - 1)
    s["weights"] = max(2, int(s.get("weights", 0)) - 1)
    return s


def adjust_focus_text(text: str | None, is_deload: bool, mode: str) -> str | None:
    if not text or mode != "dual":
        return text
    if text.startswith("Base - aerobic foundation"):
        return text + RUN_FOCUS_BASE
    return text + RUN_FOCUS[bool(is_deload)]
