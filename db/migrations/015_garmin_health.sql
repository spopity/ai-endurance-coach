-- Health data now comes straight from the Garmin Connect Personal API
-- (ingest/import_garmin.py), not via Intervals.icu. Garmin is the only writer of
-- the existing health columns (sleep_score, sleep_duration_sec, sleep_quality,
-- avg_sleeping_hr, resting_hr, hrv, spo2, respiration, steps); Intervals.icu keeps
-- the fitness model (ctl/atl/ramp_rate, sport_info), vo2max, blood pressure and the
-- subjective fields. The columns below are the Garmin fields Intervals.icu never had.

ALTER TABLE daily_metrics
    -- sleep (the night that ends on `day`)
    ADD COLUMN IF NOT EXISTS sleep_start              TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS sleep_end                TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS sleep_deep_sec           INT,
    ADD COLUMN IF NOT EXISTS sleep_light_sec          INT,
    ADD COLUMN IF NOT EXISTS sleep_rem_sec            INT,
    ADD COLUMN IF NOT EXISTS sleep_awake_sec          INT,
    ADD COLUMN IF NOT EXISTS sleep_unmeasurable_sec   INT,
    ADD COLUMN IF NOT EXISTS nap_sec                  INT,
    ADD COLUMN IF NOT EXISTS sleep_awake_count        SMALLINT,
    ADD COLUMN IF NOT EXISTS sleep_stress             DOUBLE PRECISION,   -- avg stress while asleep (0-100)
    ADD COLUMN IF NOT EXISTS sleep_respiration        DOUBLE PRECISION,   -- breaths/min, avg while asleep
    ADD COLUMN IF NOT EXISTS sleep_respiration_low    DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS sleep_respiration_high   DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS sleep_feedback           TEXT,               -- e.g. POSITIVE_LONG_AND_CONTINUOUS

    -- HRV status (Garmin's own 7-day average and personal baseline band)
    ADD COLUMN IF NOT EXISTS hrv_weekly_avg           DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS hrv_5min_high            DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS hrv_status               TEXT,               -- BALANCED | UNBALANCED | LOW | POOR
    ADD COLUMN IF NOT EXISTS hrv_baseline_low         DOUBLE PRECISION,   -- balanced band, lower edge
    ADD COLUMN IF NOT EXISTS hrv_baseline_high        DOUBLE PRECISION,   -- balanced band, upper edge
    ADD COLUMN IF NOT EXISTS hrv_low_upper            DOUBLE PRECISION,   -- below this = LOW

    -- heart rate over the whole day
    ADD COLUMN IF NOT EXISTS resting_hr_7d_avg        INT,
    ADD COLUMN IF NOT EXISTS min_hr                   INT,
    ADD COLUMN IF NOT EXISTS max_hr                   INT,

    -- all-day stress (Garmin 0-100 scale; the old `stress` column is the subjective 1-5 check-in)
    ADD COLUMN IF NOT EXISTS stress_avg               SMALLINT,
    ADD COLUMN IF NOT EXISTS stress_max               SMALLINT,
    ADD COLUMN IF NOT EXISTS stress_rest_sec          INT,
    ADD COLUMN IF NOT EXISTS stress_low_sec           INT,
    ADD COLUMN IF NOT EXISTS stress_medium_sec        INT,
    ADD COLUMN IF NOT EXISTS stress_high_sec          INT,
    ADD COLUMN IF NOT EXISTS stress_qualifier         TEXT,

    -- Body Battery (0-100)
    ADD COLUMN IF NOT EXISTS body_battery_wake        SMALLINT,
    ADD COLUMN IF NOT EXISTS body_battery_high        SMALLINT,
    ADD COLUMN IF NOT EXISTS body_battery_low         SMALLINT,
    ADD COLUMN IF NOT EXISTS body_battery_charged     SMALLINT,
    ADD COLUMN IF NOT EXISTS body_battery_drained     SMALLINT,

    -- respiration while awake (the existing `respiration` column is the awake average)
    ADD COLUMN IF NOT EXISTS respiration_low          DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS respiration_high         DOUBLE PRECISION,

    -- pulse ox (the existing `spo2` column is the daily average)
    ADD COLUMN IF NOT EXISTS spo2_lowest              DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS spo2_sleep_avg           DOUBLE PRECISION,

    -- daily activity
    ADD COLUMN IF NOT EXISTS total_kcal               INT,
    ADD COLUMN IF NOT EXISTS active_kcal              INT,
    ADD COLUMN IF NOT EXISTS intensity_min_moderate   INT,
    ADD COLUMN IF NOT EXISTS intensity_min_vigorous   INT,
    ADD COLUMN IF NOT EXISTS floors_ascended          INT,
    ADD COLUMN IF NOT EXISTS wellness_distance_m      DOUBLE PRECISION,

    ADD COLUMN IF NOT EXISTS fitness_age              DOUBLE PRECISION,

    ADD COLUMN IF NOT EXISTS garmin_payload           JSONB,              -- Garmin responses for the day, minus intraday samples
    ADD COLUMN IF NOT EXISTS garmin_updated_at        TIMESTAMPTZ;
