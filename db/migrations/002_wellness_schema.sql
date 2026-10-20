-- Replaces the initial 8-column daily_metrics with the real Intervals.icu
-- wellness field set (46 fields observed). Safe to DROP/CREATE: table is
-- still empty at this point, no backfill has run against it yet.

DROP TABLE daily_metrics;

CREATE TABLE daily_metrics (
    day                 DATE PRIMARY KEY,

    -- Intervals.icu's own authoritative daily fitness/fatigue series
    -- (computed across every sport, not just power-based ones)
    ctl                 DOUBLE PRECISION,
    atl                 DOUBLE PRECISION,
    ramp_rate           DOUBLE PRECISION,

    -- sleep
    sleep_score         DOUBLE PRECISION,
    sleep_duration_sec  INT,
    sleep_quality       SMALLINT,
    avg_sleeping_hr     DOUBLE PRECISION,

    -- resting physiology
    resting_hr          INT,
    hrv                 DOUBLE PRECISION,      -- Intervals.icu 'hrv' (rMSSD-based, ms)
    hrv_sdnn            DOUBLE PRECISION,       -- separate SDNN-method reading
    spo2                DOUBLE PRECISION,
    respiration         DOUBLE PRECISION,
    systolic            INT,
    diastolic           INT,
    vo2max              DOUBLE PRECISION,

    -- subjective daily check-in (all null today - nothing currently logs these,
    -- kept ready for whenever that starts)
    soreness            SMALLINT,
    fatigue             SMALLINT,
    stress              SMALLINT,
    mood                SMALLINT,
    motivation          SMALLINT,
    readiness           DOUBLE PRECISION,
    injury              SMALLINT,

    -- body / nutrition
    weight_kg           DOUBLE PRECISION,
    body_fat_pct        DOUBLE PRECISION,
    kcal_consumed       INT,
    steps               INT,

    comments            TEXT,
    sport_info          JSONB,      -- per-sport eFTP/W'/Pmax on that day, as reported
    raw_payload         JSONB,      -- full untouched wellness record

    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
