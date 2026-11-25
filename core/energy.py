"""
Low energy availability check: is the athlete eating enough for the training?

Too little energy for the load (low energy availability, the cause of RED-S - Mountjoy et al.,
IOC consensus 2018/2023) shows first as body weight drifting down while the training load goes
up; performance stalls and illness/injury follow weeks later. The scale (Withings) and the load
(Intervals.icu CTL) are enough to catch the early sign:

- weight: the median of the weigh-ins in the last RECENT_DAYS vs the median of the weigh-ins
  in the BEFORE_DAYS before that (medians: robust to a salty dinner or a hard session the day
  before); at least MIN_WEIGHINS in each window, otherwise "not enough weigh-ins",
- load: CTL now vs CTL LOAD_DAYS ago, rising = at least CTL_RISE,
- flag ("watch") when the weight fell by DROP_PCT or more while the load rose; a bigger fall
  (BIG_DROP_PCT) flags even without a load rise. HRV under its 4-week normal is added as a second
  sign when it is there (it also falls with plain fatigue, so it never flags on its own).

What it changes: the daily carbs of the 7-day plan move to the top of their range
(core/nutrition.daily_carbs), a Telegram message when the flag starts, and the coach sees it.
Readiness is not touched. Weight loss on purpose would also flag - eat more around training
then, or tell the coach.
"""
import statistics
from datetime import date, timedelta

RECENT_DAYS, BEFORE_DAYS = 21, 42
MIN_WEIGHINS = 2
LOAD_DAYS = 28
CTL_RISE = 3.0
DROP_PCT, BIG_DROP_PCT = 1.5, 2.5
HRV_LOW_PCT = 7.0
WEIGH_IN_STALE_DAYS = 14


def check(weights: dict[date, float], ctl: dict[date, float], hrv: dict[date, float], today: date) -> dict:
    """{level: ok | watch | unknown, text, weight_change_pct, weight_change_kg, ctl_change, last_weigh_in}."""
    recent = [w for d, w in weights.items() if today - timedelta(days=RECENT_DAYS) < d <= today]
    before = [w for d, w in weights.items()
              if today - timedelta(days=RECENT_DAYS + BEFORE_DAYS) < d <= today - timedelta(days=RECENT_DAYS)]
    last = max(weights, default=None)
    out = {"level": "unknown", "weight_change_pct": None, "weight_change_kg": None, "ctl_change": None,
           "last_weigh_in": last.isoformat() if last else None}
    c_now = next((ctl[d] for d in (today - timedelta(days=i) for i in range(3)) if ctl.get(d) is not None), None)
    c_then = next((ctl[d] for d in (today - timedelta(days=LOAD_DAYS + i) for i in range(3))
                   if ctl.get(d) is not None), None)
    if c_now is not None and c_then is not None:
        out["ctl_change"] = round(c_now - c_then, 1)
    if len(recent) < MIN_WEIGHINS or len(before) < MIN_WEIGHINS:
        out["text"] = (f"Low-energy check: not enough weigh-ins ({len(recent)} in the last {RECENT_DAYS} days, "
                       f"{len(before)} in the {BEFORE_DAYS} before; it needs {MIN_WEIGHINS}+ in each) - "
                       "weigh in 2-3 mornings a week")
        return out
    now_w, then_w = statistics.median(recent), statistics.median(before)
    pct = (now_w / then_w - 1) * 100
    out.update(weight_change_pct=round(pct, 1), weight_change_kg=round(now_w - then_w, 1))
    rising = out["ctl_change"] is not None and out["ctl_change"] >= CTL_RISE
    h7 = [hrv[d] for d in (today - timedelta(days=i) for i in range(7)) if hrv.get(d) is not None]
    h28 = [hrv[d] for d in (today - timedelta(days=i) for i in range(7, 35)) if hrv.get(d) is not None]
    hrv_low = len(h7) >= 4 and len(h28) >= 10 and \
        statistics.fmean(h7) <= statistics.fmean(h28) * (1 - HRV_LOW_PCT / 100)
    load = f"CTL {out['ctl_change']:+.0f} in {LOAD_DAYS} days" if out["ctl_change"] is not None else "load unknown"
    weight = f"weight {now_w - then_w:+.1f} kg ({pct:+.1f}%)"
    if (-pct >= DROP_PCT and rising) or -pct >= BIG_DROP_PCT:
        out["level"] = "watch"
        out["text"] = (f"Low-energy flag: {weight} while {load}" + ("; HRV below its normal too" if hrv_low else "")
                       + " - eat more around training (top of the daily carb range), don't diet in a build")
    else:
        out["level"] = "ok"
        out["text"] = f"Energy availability OK: {weight}, {load}"
    return out


def current(today: date | None = None) -> dict:
    from core.overview import query
    today = today or date.today()
    since = today - timedelta(days=RECENT_DAYS + BEFORE_DAYS + 7)
    rows = query("SELECT day, weight_kg, ctl, hrv FROM daily_metrics WHERE day BETWEEN %s AND %s;", (since, today))
    pick = lambda f: {r["day"]: float(r[f]) for r in rows if r[f] is not None}  # noqa: E731
    return check(pick("weight_kg"), pick("ctl"), pick("hrv"), today)


def low_energy(today: date | None = None) -> bool:
    try:
        from core.overview import cached
        return cached("energy", 600, lambda: current(today))["level"] == "watch"
    except Exception:
        return False
