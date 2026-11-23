"""
Illness early warning from the night's numbers (Garmin), before the athlete feels ill.

Infections show up in the overnight data 1-3 days before symptoms (Mishra et al., Nature
Biomedical Engineering 2020; Natarajan et al., npj Digital Medicine 2020; Whoop/Oura research on
respiratory rate): resting heart rate up, HRV down, breathing rate up. Each night is compared with
the athlete's own normal - the 28 nights before, skipping the last 2 (so a brewing infection
doesn't drag its own baseline) - and four signals are read:

- heart:     overnight HR (fallback: resting HR) >= normal + max(3 bpm, 2 SD)
- hrv:       HRV <= normal - max(5 ms, 1.5 SD)
- stress:    Garmin sleep stress >= normal + max(8, 2 SD)  (HRV-derived: counts with hrv as one
             "autonomic" signal, never as two)
- breathing: respiration while asleep >= normal + max(1 breath/min, 2 SD)

Three independent systems: heart, autonomic (hrv/stress), breathing. Levels:
- watch:   2 of the 3 last night, or the autonomic signal 2 nights running -> amber (trim the day);
  with the athlete's "a bit off" check-in it becomes red (core/readiness.py)
- warning: all 3 last night, or "watch" 2 nights running with heart or breathing still off last
  night -> red (rest or very easy). HRV alone stays amber: it also drops after hard training.
Backtested on 13 months of the athlete's nights: about 10 watch days a year, mostly after heavy
blocks or short nights, and it flagged a real illness on its first day; some illnesses barely
show in the numbers (or the data is missing), hence the check-in answer. One bad night alone (a late night, alcohol, a hard session) stays a note. Missing data (the
watch off the wrist) just removes that signal; the athlete's "ill" check-in is handled in
core/feedback.py.
"""
import statistics
from datetime import date, timedelta

BASELINE_DAYS, SKIP_DAYS, MIN_BASELINE = 28, 2, 10
RULES = {   # field(s), direction, absolute minimum change, SD multiple
    "heart": (("avg_sleeping_hr", "resting_hr"), +1, 3.0, 2.0),
    "hrv": (("hrv",), -1, 5.0, 1.5),
    "stress": (("sleep_stress",), +1, 8.0, 2.0),
    "breathing": (("sleep_respiration",), +1, 1.0, 2.0),
}
TEXT = {"heart": "overnight heart rate up", "hrv": "HRV down", "stress": "sleep stress up",
        "breathing": "breathing rate up"}


def _value(row: dict, fields: tuple) -> tuple[str, float] | None:
    for f in fields:
        if row.get(f) is not None:
            return f, float(row[f])
    return None


def night_signals(rows: dict, day: date) -> dict[str, str]:
    """{signal: short text} that fired on `day`. `rows` maps day -> daily_metrics row."""
    today = rows.get(day)
    if not today:
        return {}
    out = {}
    for name, (fields, sign, min_abs, sds) in RULES.items():
        v = _value(today, fields)
        if not v:
            continue
        field, x = v
        base = [float(r[field]) for d in (day - timedelta(days=i) for i in range(SKIP_DAYS + 1, SKIP_DAYS + 1 + BASELINE_DAYS))
                if (r := rows.get(d)) and r.get(field) is not None]
        if len(base) < MIN_BASELINE:
            continue
        mean, sd = statistics.fmean(base), statistics.pstdev(base)
        delta = (x - mean) * sign
        if delta >= max(min_abs, sds * sd):
            unit = {"heart": " bpm", "hrv": " ms", "breathing": "/min"}.get(name, "")
            out[name] = f"{TEXT[name]} ({x:.0f}{unit} vs {mean:.0f})" if name != "breathing" else \
                f"{TEXT[name]} ({x:.1f}{unit} vs {mean:.1f})"
    return out


def _systems(sig: dict) -> set:
    return {"autonomic" if k in ("hrv", "stress") else k for k in sig}


def _watch(sig: dict, prev: dict) -> bool:
    s = _systems(sig)
    return len(s) >= 2 or ("autonomic" in s and "autonomic" in _systems(prev))


def status(rows: dict, day: date) -> dict:
    """{level: none|note|watch|warning, signals: [...], text} for the night ending on `day`."""
    sig, prev = night_signals(rows, day), night_signals(rows, day - timedelta(days=1))
    prev2 = night_signals(rows, day - timedelta(days=2))
    watch_now, watch_prev = _watch(sig, prev), _watch(prev, prev2)
    if len(_systems(sig)) >= 3 or (watch_now and watch_prev and _systems(sig) & {"heart", "breathing"}):
        level = "warning"
    elif watch_now:
        level = "watch"
    elif sig:
        level = "note"
    else:
        level = "none"
    text = {"warning": "possible illness coming on - your body is fighting something",
            "watch": "early illness signs (or a rough night)",
            "note": "one-off dip in last night's numbers"}.get(level)
    return {"level": level, "signals": list(sig.values()), "text": text}


def load_rows(days: int = 40, until: date | None = None) -> dict:
    from core.overview import query
    until = until or date.today()
    rows = query("""SELECT day, avg_sleeping_hr, resting_hr, hrv, sleep_stress, sleep_respiration
                    FROM daily_metrics WHERE day BETWEEN %s AND %s;""", (until - timedelta(days=days), until))
    return {r["day"]: r for r in rows}


def current(today: date | None = None) -> dict:
    today = today or date.today()
    return status(load_rows(until=today), today)
