"""
Did the devices record what they normally do? A watch that quietly stops measuring something
(wrist heart rate switched off, not worn at night, a chest strap with a flat battery) leaves the
coach blind - readiness, the illness warning and the thresholds all run on that data. This asks
the athlete to check the device, via Telegram (scripts/check_health.py, hourly; once per problem,
and once more when it's back).

Compared with the athlete's own normal: a measurement only counts as missing when it was there
on at least USUAL_SHARE of the BASELINE_DAYS before.

- watch, overnight (the last MISSING_NIGHTS synced nights - a night that hasn't synced yet doesn't
  count; no Garmin data at all is the sync check's job): heart rate (resting / sleeping HR,
  breathing and Body Battery all come from it), HRV, sleep,
- watch, daytime (the last MISSING_NIGHTS full days): steps under MIN_STEPS - not worn,
- activities of the last ACTIVITY_DAYS: a ride or run without heart rate, a ride without power,
  when the previous activities of the same kind (sport, indoor/outdoor) had it,
- the scale: no weigh-in for WEIGH_IN_DAYS when the athlete weighs in regularly (the low-energy
  check in core/energy.py needs it).
"""
from datetime import date, timedelta

BASELINE_DAYS = 28
USUAL_SHARE = 0.7
MISSING_NIGHTS = 2
MIN_STEPS = 500
ACTIVITY_DAYS = 3
ACTIVITY_BASELINE = 10
WEIGH_IN_DAYS = 14
GROUPS = {  # key: (fields - missing when ALL are missing, what, what to check)
    "heart": (("resting_hr", "avg_sleeping_hr", "min_hr"), "heart rate",
              "is wrist heart rate switched on (Settings > Health & Wellness > Wrist Heart Rate) and the watch snug "
              "above the wrist bone?"),
    "hrv": (("hrv",), "overnight HRV", "did you wear it to sleep, with enough battery?"),
    "sleep": (("sleep_duration_sec",), "sleep", "did you wear it to sleep, with enough battery?"),
}
ENDURANCE = {"Ride": "ride", "VirtualRide": "ride", "GravelRide": "ride", "MountainBikeRide": "ride",
             "Run": "run", "TrailRun": "run", "VirtualRun": "run"}


def _usual(rows: dict, fields: tuple, days: list[date]) -> bool:
    seen = [d for d in days if d in rows]
    have = sum(1 for d in seen if any(rows[d].get(f) is not None for f in fields))
    return len(seen) >= BASELINE_DAYS // 2 and have >= USUAL_SHARE * len(seen)


def daily_gaps(rows: dict, today: date) -> dict[str, str]:
    """{key: message} for watch measurements missing. `rows`: day -> daily_metrics row (with
    `garmin` True when Garmin synced that day)."""
    out = {}
    synced = sorted((d for d, r in rows.items() if r.get("garmin") and d <= today), reverse=True)
    for key, (fields, what, hint) in GROUPS.items():
        streak = []   # the synced nights, newest first, that are missing it
        for d in synced:
            if any(rows[d].get(f) is not None for f in fields):
                break
            streak.append(d)
        if len(streak) < MISSING_NIGHTS:
            continue
        # The normal is the 4 weeks before the gap began - a long gap doesn't become the normal.
        base_days = [streak[-1] - timedelta(days=i) for i in range(1, BASELINE_DAYS + 1)]
        if _usual(rows, fields, base_days):
            out[f"watch:{key}"] = (f"⌚ Your watch has recorded no {what} for {len(streak)} nights "
                                   f"(since {streak[-1]:%d %b}) - {hint}")
    full = [today - timedelta(days=i) for i in range(1, MISSING_NIGHTS + 1)]
    if all(d in rows and rows[d].get("garmin") for d in full):
        low = all((rows[d].get("steps") or 0) < MIN_STEPS for d in full)
        base = [today - timedelta(days=i) for i in range(MISSING_NIGHTS + 1, MISSING_NIGHTS + 1 + BASELINE_DAYS)]
        usual = [d for d in base if d in rows and (rows[d].get("steps") or 0) >= MIN_STEPS]
        if low and len(usual) >= USUAL_SHARE * BASELINE_DAYS:
            out["watch:steps"] = (f"⌚ Almost no steps on your watch for {MISSING_NIGHTS} days (since "
                                  f"{full[-1]:%d %b}) - is it on your wrist during the day?")
    return out


def activity_gaps(recent: list[dict], history: list[dict]) -> dict[str, str]:
    """{key: message} for recent activities missing heart rate or power that the same kind of
    activity normally has. Rows: {id, day, type, trainer, name, avg_hr, avg_power_w}."""
    out = {}

    def kind(a):
        return ENDURANCE.get(a["type"]), bool(a.get("trainer")) or a["type"].startswith("Virtual")
    for a in recent:
        sport, indoor = kind(a)
        if not sport:
            continue
        same = [h for h in history if kind(h) == (sport, indoor) and h["id"] != a["id"]][:ACTIVITY_BASELINE]
        if len(same) < 3:
            continue
        where = "indoor " if indoor else ""
        label = f"your {where}{sport} '{a['name']}' on {a['day']:%d %b}"
        if a.get("avg_hr") is None and sum(h.get("avg_hr") is not None for h in same) >= USUAL_SHARE * len(same):
            out[f"act:{a['id']}:hr"] = (f"⌚ No heart rate on {label} - is the chest strap paired to the "
                                        "device that records, with a good battery and wet contacts?")
        if sport == "ride" and a.get("avg_power_w") is None and \
                sum(h.get("avg_power_w") is not None for h in same) >= USUAL_SHARE * len(same):
            out[f"act:{a['id']}:power"] = (f"⌚ No power on {label} - " + (
                "was the trainer connected to the app?" if indoor else "is the power meter paired, battery OK?"))
    return out


def scale_gap(weigh_ins: list[date], today: date) -> dict[str, str]:
    """No weigh-in for WEIGH_IN_DAYS although the athlete weighed in at least twice in the 2 months before."""
    if not weigh_ins:
        return {}
    last = max(weigh_ins)
    before = [d for d in weigh_ins if today - timedelta(days=WEIGH_IN_DAYS + 60) <= d < today - timedelta(days=WEIGH_IN_DAYS)]
    if (today - last).days >= WEIGH_IN_DAYS and len(before) >= 2:
        return {"scale": (f"⚖️ No weigh-in since {last:%d %b} - step on the scale 2-3 mornings a week; the "
                          "low-energy check runs on the weight trend")}
    return {}


def check(today: date | None = None) -> dict[str, str]:
    from core.overview import query
    from core.schedule import TZ
    today = today or date.today()
    since = today - timedelta(days=BASELINE_DAYS + 60)
    rows = query("""SELECT day, resting_hr, avg_sleeping_hr, min_hr, hrv, sleep_duration_sec, steps,
                           garmin_updated_at IS NOT NULL AS garmin
                    FROM daily_metrics WHERE day BETWEEN %s AND %s;""", (since, today))
    out = daily_gaps({r["day"]: r for r in rows}, today)
    acts = query("""SELECT id, (start_time AT TIME ZONE %s)::date AS day, type, trainer, name,
                           avg_hr, avg_power_w, moving_time_sec
                    FROM activities WHERE superseded_by IS NULL AND start_time >= %s AND moving_time_sec >= 600
                    ORDER BY start_time DESC;""", (str(TZ), today - timedelta(days=120)))
    recent = [a for a in acts if a["day"] >= today - timedelta(days=ACTIVITY_DAYS)]
    out.update(activity_gaps(recent, [a for a in acts if a["day"] < today - timedelta(days=ACTIVITY_DAYS)]))
    w = query("SELECT day FROM daily_metrics WHERE weight_kg IS NOT NULL AND day >= %s;", (today - timedelta(days=120),))
    out.update(scale_gap([r["day"] for r in w], today))
    return out
