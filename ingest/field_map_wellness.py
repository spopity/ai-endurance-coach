"""
Maps a raw Intervals.icu wellness record (GET /athlete/{id}/wellness.json)
onto the ai-coach-v1 `daily_metrics` row shape.

Health fields (sleep, HRV, resting HR, SpO2, respiration, steps) are not mapped:
they come from Garmin directly (ingest/import_garmin.py), the only writer of them.
"""
from datetime import date
from typing import Optional, Any
from pydantic import BaseModel

SPORT_INFO_FIELD = "sportInfo"


class WellnessRecord(BaseModel):
    day: date
    ctl: Optional[float] = None
    atl: Optional[float] = None
    ramp_rate: Optional[float] = None

    systolic: Optional[int] = None
    diastolic: Optional[int] = None
    vo2max: Optional[float] = None

    soreness: Optional[int] = None
    fatigue: Optional[int] = None
    stress: Optional[int] = None
    mood: Optional[int] = None
    motivation: Optional[int] = None
    readiness: Optional[float] = None
    injury: Optional[int] = None

    weight_kg: Optional[float] = None
    body_fat_pct: Optional[float] = None
    kcal_consumed: Optional[int] = None

    comments: Optional[str] = None
    sport_info: Any = None
    raw_payload: dict[str, Any] = {}


def _int_or_none(v):
    return int(v) if v is not None else None


def map_wellness(raw: dict) -> WellnessRecord:
    """Maps one raw Intervals.icu wellness dict to a WellnessRecord."""
    return WellnessRecord(
        day=raw["id"],
        ctl=raw.get("ctl"),
        atl=raw.get("atl"),
        ramp_rate=raw.get("rampRate"),

        systolic=_int_or_none(raw.get("systolic")),
        diastolic=_int_or_none(raw.get("diastolic")),
        vo2max=raw.get("vo2max"),

        soreness=_int_or_none(raw.get("soreness")),
        fatigue=_int_or_none(raw.get("fatigue")),
        stress=_int_or_none(raw.get("stress")),
        mood=_int_or_none(raw.get("mood")),
        motivation=_int_or_none(raw.get("motivation")),
        readiness=raw.get("readiness"),
        injury=_int_or_none(raw.get("injury")),

        weight_kg=raw.get("weight"),
        body_fat_pct=raw.get("bodyFat"),
        kcal_consumed=_int_or_none(raw.get("kcalConsumed")),

        comments=raw.get("comments"),
        sport_info=raw.get(SPORT_INFO_FIELD),
        raw_payload=raw,
    )
