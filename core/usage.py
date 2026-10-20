"""
Aggregates the api_calls log (core/api_log.py) for the dashboard's System page:
calls per service over time, Gemini tokens and estimated cost, who triggers what,
and the most recent calls.
"""
from datetime import datetime, timedelta, timezone

from core.api_log import DEFAULT_PRICE, PRICES
from core.overview import query

TZ = "Europe/Bratislava"
RECENT_LIMIT = 80


def _f(v, digits=6):
    return round(float(v), digits) if v is not None else 0.0


def usage_summary(days: int) -> dict:
    days = max(1, min(days, 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    hourly = days <= 2  # short ranges are plotted per hour, longer ones per day
    bucket = "hour" if hourly else "day"

    totals = query("""
        SELECT service, COUNT(*) AS calls, COUNT(*) FILTER (WHERE NOT ok) AS errors,
               COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(thinking_tokens), 0) AS thinking_tokens, COALESCE(SUM(cost_usd), 0) AS cost,
               AVG(duration_ms) AS avg_ms
        FROM api_calls WHERE ts >= %s GROUP BY service ORDER BY calls DESC;
    """, (since,))

    series = query(f"""
        WITH buckets AS (
            SELECT generate_series(date_trunc('{bucket}', %s AT TIME ZONE '{TZ}'),
                                   date_trunc('{bucket}', now() AT TIME ZONE '{TZ}'), interval '1 {bucket}') AS t
        )
        SELECT b.t, c.service, COUNT(c.id) AS calls, COALESCE(SUM(c.cost_usd), 0) AS cost
        FROM buckets b
        LEFT JOIN api_calls c ON c.ts >= %s AND date_trunc('{bucket}', c.ts AT TIME ZONE '{TZ}') = b.t
        GROUP BY b.t, c.service ORDER BY b.t;
    """, (since, since))
    points: dict = {}
    for r in series:
        p = points.setdefault(r["t"], {"t": r["t"].isoformat(), "calls": {}, "cost": 0.0})
        if r["service"]:
            p["calls"][r["service"]] = r["calls"]
            p["cost"] += float(r["cost"])
    timeline = [{**p, "cost": round(p["cost"], 6)} for p in points.values()]

    components = query("""
        SELECT service, COALESCE(component, '-') AS component, COUNT(*) AS calls,
               COUNT(*) FILTER (WHERE NOT ok) AS errors,
               COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cost_usd), 0) AS cost, AVG(duration_ms) AS avg_ms
        FROM api_calls WHERE ts >= %s AND service IN ('gemini', 'ollama')
        GROUP BY 1, 2 ORDER BY cost DESC, calls DESC;
    """, (since,))

    triggers = query("""
        SELECT COALESCE(trigger, 'other') AS trigger, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost
        FROM api_calls WHERE ts >= %s GROUP BY 1 ORDER BY calls DESC;
    """, (since,))

    # process -> trigger -> service: the three columns of the "how the system runs" flow chart
    flows = query("""
        SELECT process, COALESCE(trigger, 'other') AS trigger, service, COUNT(*) AS calls
        FROM api_calls WHERE ts >= %s GROUP BY 1, 2, 3;
    """, (since,))

    hours = query(f"""
        SELECT EXTRACT(HOUR FROM ts AT TIME ZONE '{TZ}')::int AS hour, service, COUNT(*) AS calls
        FROM api_calls WHERE ts >= %s GROUP BY 1, 2;
    """, (since,))
    by_hour = [{"hour": h, "calls": {}} for h in range(24)]
    for r in hours:
        by_hour[r["hour"]]["calls"][r["service"]] = r["calls"]

    recent = query("""
        SELECT ts, service, operation, component, trigger, process, model, status, ok, duration_ms,
               input_tokens, output_tokens, thinking_tokens, cost_usd, error
        FROM api_calls ORDER BY id DESC LIMIT %s;
    """, (RECENT_LIMIT,))

    first = query("SELECT MIN(ts) AS t FROM api_calls;")[0]["t"]
    gemini = next((t for t in totals if t["service"] == "gemini"), None)
    cost = float(gemini["cost"]) if gemini else 0.0
    # Project from the days actually covered by the log, not the requested range - but at least a
    # full day, so a few minutes of logging with one Gemini call doesn't extrapolate to a huge month.
    covered_days = max(1.0, min(days, (datetime.now(timezone.utc) - first).total_seconds() / 86400)) if first else None
    price_in, price_out, price_cached = PRICES[DEFAULT_PRICE]["base"]

    return {
        "days": days,
        "bucket": bucket,
        "logging_since": first.isoformat() if first else None,
        "totals": [{"service": t["service"], "calls": t["calls"], "errors": t["errors"],
                    "avg_ms": round(float(t["avg_ms"])) if t["avg_ms"] is not None else None} for t in totals],
        "gemini": {
            "calls": gemini["calls"] if gemini else 0,
            "input_tokens": int(gemini["input_tokens"]) if gemini else 0,
            "output_tokens": int(gemini["output_tokens"]) if gemini else 0,
            "thinking_tokens": int(gemini["thinking_tokens"]) if gemini else 0,
            "cost": round(cost, 4),
            "avg_cost_per_call": round(cost / gemini["calls"], 4) if gemini and gemini["calls"] else None,
            "projected_month": round(cost / covered_days * 30, 2) if covered_days else None,
            "price": {"model": DEFAULT_PRICE, "input_per_m": price_in, "output_per_m": price_out,
                      "cached_per_m": price_cached},
        },
        "timeline": timeline,
        "components": [{**{k: r[k] for k in ("service", "component", "calls", "errors")},
                        "input_tokens": int(r["input_tokens"]), "output_tokens": int(r["output_tokens"]),
                        "cost": _f(r["cost"], 4), "avg_ms": round(float(r["avg_ms"])) if r["avg_ms"] else None}
                       for r in components],
        "triggers": [{"trigger": r["trigger"], "calls": r["calls"], "cost": _f(r["cost"], 4)} for r in triggers],
        "flows": [dict(r) for r in flows],
        "by_hour": by_hour,
        "recent": [{**dict(r), "ts": r["ts"].isoformat(), "cost_usd": _f(r["cost_usd"]) if r["cost_usd"] is not None else None}
                   for r in recent],
    }
