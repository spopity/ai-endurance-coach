"""
Race-day weather from Open-Meteo (free, no API key):
- within the forecast horizon (~15 days): the actual forecast for that day;
- further out: typical conditions for that calendar date from the last 5 years
  of the historical archive ("climatology"), clearly labelled as such.
"""
import logging
import statistics
from datetime import date, timedelta

import requests

logger = logging.getLogger("AI_COACH_V1.weather")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_HORIZON_DAYS = 15
CLIMATE_YEARS = 5
DAILY_FIELDS = "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,wind_direction_10m_dominant"


def geocode(place: str) -> dict | None:
    """Returns {name, country, latitude, longitude, elevation} for a place name, or None."""
    if not place:
        return None
    # Try the full string first, then progressively simpler variants ("Bratislava, Slovakia" -> "Bratislava").
    candidates = [place] + [p.strip() for p in place.split(",")[:1] if p.strip() != place]
    for name in candidates:
        try:
            resp = requests.get(GEOCODE_URL, params={"name": name, "count": 1, "language": "en"}, timeout=15)
            resp.raise_for_status()
            results = resp.json().get("results") or []
        except Exception as e:
            logger.warning(f"Geocoding '{name}' failed: {e}")
            continue
        if results:
            r = results[0]
            return {"name": r.get("name"), "country": r.get("country"), "latitude": r["latitude"],
                    "longitude": r["longitude"], "elevation": r.get("elevation")}
    return None


def _wind_dir(deg) -> str:
    if deg is None:
        return "?"
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg % 360) / 45 + 0.5) % 8]


def race_weather(place: str, race_date: date) -> dict:
    """Weather for a race. Always returns a dict with 'available' and a human 'summary'."""
    loc = geocode(place)
    if not loc:
        return {"available": False, "summary": f"Could not locate '{place}' for weather."}
    lat, lon = loc["latitude"], loc["longitude"]
    where = f"{loc['name']}, {loc.get('country') or ''}".strip(", ")
    days_out = (race_date - date.today()).days

    try:
        if 0 <= days_out <= FORECAST_HORIZON_DAYS:
            resp = requests.get(FORECAST_URL, params={
                "latitude": lat, "longitude": lon, "daily": DAILY_FIELDS + ",precipitation_probability_max",
                "timezone": "auto", "start_date": race_date.isoformat(), "end_date": race_date.isoformat(),
            }, timeout=20)
            resp.raise_for_status()
            d = resp.json()["daily"]
            w = {
                "available": True, "kind": "forecast", "location": where, "elevation_m": loc.get("elevation"),
                "temp_max": d["temperature_2m_max"][0], "temp_min": d["temperature_2m_min"][0],
                "precip_mm": d["precipitation_sum"][0], "precip_prob": d["precipitation_probability_max"][0],
                "wind_max_kmh": d["wind_speed_10m_max"][0], "wind_dir": _wind_dir(d["wind_direction_10m_dominant"][0]),
            }
            w["summary"] = (
                f"Forecast for {where} on {race_date}: {w['temp_min']:.0f}-{w['temp_max']:.0f}°C, "
                f"rain {w['precip_mm']:.1f} mm ({w['precip_prob']}% chance), wind up to {w['wind_max_kmh']:.0f} km/h "
                f"from {w['wind_dir']}."
            )
            return w

        # Climatology: same date +-3 days over the last CLIMATE_YEARS years.
        tmax, tmin, precip, wind, rainy_days, total_days = [], [], [], [], 0, 0
        for years_back in range(1, CLIMATE_YEARS + 1):
            try:
                center = race_date.replace(year=race_date.year - years_back)
            except ValueError:  # 29 Feb
                center = race_date.replace(year=race_date.year - years_back, day=28)
            if center >= date.today():
                center = center.replace(year=center.year - 1)
            resp = requests.get(ARCHIVE_URL, params={
                "latitude": lat, "longitude": lon, "daily": DAILY_FIELDS, "timezone": "auto",
                "start_date": (center - timedelta(days=3)).isoformat(),
                "end_date": (center + timedelta(days=3)).isoformat(),
            }, timeout=20)
            resp.raise_for_status()
            d = resp.json()["daily"]
            tmax += [v for v in d["temperature_2m_max"] if v is not None]
            tmin += [v for v in d["temperature_2m_min"] if v is not None]
            wind += [v for v in d["wind_speed_10m_max"] if v is not None]
            p = [v for v in d["precipitation_sum"] if v is not None]
            precip += p
            rainy_days += sum(1 for v in p if v >= 1)
            total_days += len(p)
        if not tmax:
            return {"available": False, "summary": f"No historical weather data for {where}."}
        w = {
            "available": True, "kind": "typical", "location": where, "elevation_m": loc.get("elevation"),
            "temp_max": statistics.fmean(tmax), "temp_min": statistics.fmean(tmin),
            "temp_max_range": (min(tmax), max(tmax)),
            "rain_day_pct": round(100 * rainy_days / total_days) if total_days else None,
            "wind_max_kmh": statistics.fmean(wind) if wind else None,
        }
        w["summary"] = (
            f"Typical conditions for {where} around {race_date:%d %b} (last {CLIMATE_YEARS} years - not a forecast; "
            f"the real forecast becomes available ~{FORECAST_HORIZON_DAYS} days before): highs around "
            f"{w['temp_max']:.0f}°C (range {w['temp_max_range'][0]:.0f}-{w['temp_max_range'][1]:.0f}°C), lows "
            f"{w['temp_min']:.0f}°C, rain on {w['rain_day_pct']}% of days, wind gusts typically up to "
            f"{w['wind_max_kmh']:.0f} km/h."
        )
        return w
    except Exception as e:
        logger.error(f"Weather lookup failed: {e}", exc_info=True)
        return {"available": False, "summary": f"Weather lookup failed: {e}"}
