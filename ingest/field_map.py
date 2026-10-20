"""
Maps a raw Intervals.icu activity payload (GET /athlete/{id}/activities) onto
the ai-coach-v1 `activities` row shape.

Field-mapping decisions worth remembering:

- intensity_factor is stored as a true decimal ratio (NP/FTP), e.g. 0.71,
  NOT Intervals.icu's percentage-style display (icu_intensity=70.84...).
  This matches the TSS formula already used elsewhere in the project
  (TSS = duration_hours * IF^2 * 100) - storing the percentage instead would
  silently inflate every downstream TSS/CTL/ATL calculation by 10,000x.
- start_time is taken from `start_date` (absolute UTC), not `start_date_local`,
  so it's unambiguous in the database; `timezone` is kept alongside it for
  reconstructing local time when needed.
- `pace` and `gap` units haven't been confirmed against a Run-type sample yet
  (only verified on a Ride, where they're not meaningful) - treat
  pace_sec_per_km as unverified until checked against real running data.
"""
from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel

# Tier 2/3 fields worth keeping but not worth a dedicated column - stored
# as-is (source key -> value) inside `raw_metrics`. Anything not listed here
# still survives in `raw_payload`, just not broken out for easy querying.
RAW_METRICS_FIELDS = [
    # zone / time-in-zone breakdowns
    "icu_zone_times", "icu_hr_zone_times", "icu_hr_zones", "icu_power_zones",
    "pace_zones", "pace_zone_times", "gap_zone_times",
    # power-duration model
    "icu_pm_ftp", "icu_pm_ftp_secs", "icu_pm_ftp_watts",
    "icu_rolling_cp", "icu_rolling_w_prime", "icu_rolling_p_max",
    "icu_rolling_ftp", "icu_rolling_ftp_delta",
    "icu_sweet_spot_min", "icu_sweet_spot_max",
    "icu_cadence_z2", "icu_power_hr", "icu_power_hr_z2", "icu_power_hr_z2_mins",
    # heart-rate recovery / load
    "icu_hrr", "polarization_index", "strain_score",
    # auto-detected intervals
    "interval_summary", "icu_lap_count", "icu_intervals_edited",
    # running dynamics
    "average_stance_time", "average_vertical_oscillation", "average_vertical_ratio",
    "average_step_length", "average_stance_time_percent", "average_stance_time_balance",
    "average_vertical_speed", "average_leg_spring_stiffness",
    "average_impact_loading_rate", "average_stride",
    "gap", "gap_model", "threshold_pace",
    # swim
    "pool_length", "lengths",
    # strength
    "kg_lifted",
    # equipment
    "power_meter", "power_meter_serial", "power_meter_battery", "crank_length",
    "device_watts", "gear",
    # other subjective/derived
    "perceived_exertion", "session_rpe", "compliance",
    "icu_joules", "icu_joules_above_ftp", "icu_max_wbal_depletion",
    "icu_median_time_delta", "icu_recording_time", "icu_warmup_time", "icu_cooldown_time",
    "has_heartrate", "has_weather", "has_segments", "stream_types",
]


class ActivityRecord(BaseModel):
    source: str
    external_id: str
    name: Optional[str] = None
    type: str
    sub_type: Optional[str] = None
    device_name: Optional[str] = None
    start_time: datetime
    timezone: Optional[str] = None
    commute: Optional[bool] = None
    race: Optional[bool] = None
    trainer: Optional[bool] = None

    elapsed_time_sec: Optional[int] = None
    moving_time_sec: Optional[int] = None
    coasting_time_sec: Optional[int] = None
    distance_m: Optional[float] = None
    total_elevation_gain_m: Optional[float] = None
    total_elevation_loss_m: Optional[float] = None
    average_speed_mps: Optional[float] = None
    max_speed_mps: Optional[float] = None
    pace_sec_per_km: Optional[float] = None

    avg_power_w: Optional[float] = None
    normalized_power_w: Optional[float] = None
    intensity_factor: Optional[float] = None
    variability_index: Optional[float] = None
    training_load: Optional[float] = None
    efficiency_factor: Optional[float] = None
    decoupling_pct: Optional[float] = None
    ftp_at_time: Optional[int] = None
    critical_power_w: Optional[int] = None
    w_prime_j: Optional[int] = None
    p_max_w: Optional[int] = None
    avg_lr_balance: Optional[float] = None
    avg_cadence: Optional[float] = None

    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    resting_hr_at_time: Optional[int] = None
    lthr: Optional[int] = None
    trimp: Optional[float] = None
    hr_load: Optional[float] = None
    hr_load_type: Optional[str] = None

    icu_ctl_at_time: Optional[float] = None
    icu_atl_at_time: Optional[float] = None

    feel: Optional[int] = None
    rpe: Optional[int] = None

    calories: Optional[int] = None

    raw_metrics: dict[str, Any] = {}
    raw_payload: dict[str, Any] = {}


def _int_or_none(v):
    return int(v) if v is not None else None


def map_activity(raw: dict) -> ActivityRecord:
    """Maps one raw Intervals.icu activity dict to an ActivityRecord."""
    icu_intensity = raw.get("icu_intensity")

    return ActivityRecord(
        source="intervals_icu",
        external_id=str(raw["id"]),
        name=raw.get("name"),
        type=raw["type"],
        sub_type=raw.get("sub_type"),
        device_name=raw.get("device_name"),
        start_time=raw["start_date"],
        timezone=raw.get("timezone"),
        commute=raw.get("commute"),
        race=raw.get("race"),
        trainer=raw.get("trainer"),

        elapsed_time_sec=_int_or_none(raw.get("elapsed_time")),
        moving_time_sec=_int_or_none(raw.get("moving_time")),
        coasting_time_sec=_int_or_none(raw.get("coasting_time")),
        distance_m=raw.get("distance"),
        total_elevation_gain_m=raw.get("total_elevation_gain"),
        total_elevation_loss_m=raw.get("total_elevation_loss"),
        average_speed_mps=raw.get("average_speed"),
        max_speed_mps=raw.get("max_speed"),
        pace_sec_per_km=raw.get("pace"),

        avg_power_w=raw.get("icu_average_watts"),
        normalized_power_w=raw.get("icu_weighted_avg_watts"),
        intensity_factor=(icu_intensity / 100.0) if icu_intensity is not None else None,
        variability_index=raw.get("icu_variability_index"),
        training_load=raw.get("icu_training_load"),
        efficiency_factor=raw.get("icu_efficiency_factor"),
        decoupling_pct=raw.get("decoupling"),
        ftp_at_time=_int_or_none(raw.get("icu_ftp")),
        critical_power_w=_int_or_none(raw.get("icu_pm_cp")),
        w_prime_j=_int_or_none(raw.get("icu_pm_w_prime")),
        p_max_w=_int_or_none(raw.get("icu_pm_p_max")),
        avg_lr_balance=raw.get("avg_lr_balance"),
        avg_cadence=raw.get("average_cadence"),

        avg_hr=_int_or_none(raw.get("average_heartrate")),
        max_hr=_int_or_none(raw.get("max_heartrate")),
        resting_hr_at_time=_int_or_none(raw.get("icu_resting_hr")),
        lthr=_int_or_none(raw.get("lthr")),
        trimp=raw.get("trimp"),
        hr_load=raw.get("hr_load"),
        hr_load_type=raw.get("hr_load_type"),

        icu_ctl_at_time=raw.get("icu_ctl"),
        icu_atl_at_time=raw.get("icu_atl"),

        feel=_int_or_none(raw.get("feel")),
        rpe=_int_or_none(raw.get("icu_rpe")),

        calories=_int_or_none(raw.get("calories")),

        raw_metrics={k: raw[k] for k in RAW_METRICS_FIELDS if raw.get(k) is not None},
        raw_payload=raw,
    )
