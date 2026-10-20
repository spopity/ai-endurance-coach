-- ai-coach-v1 initial schema
-- Runs inside the NEW database (athlete_twin_v2) only. Never touches athlete_twin.

CREATE TABLE athlete_profile (
    id              INT PRIMARY KEY DEFAULT 1,
    ftp             INT,
    fthr            INT,
    weight_kg       DOUBLE PRECISION,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT single_row CHECK (id = 1)
);

CREATE TABLE daily_metrics (
    day                 DATE PRIMARY KEY,
    sleep_score         INT,
    sleep_duration_sec  INT,
    avg_sleep_stress    DOUBLE PRECISION,
    hrv_rmssd           DOUBLE PRECISION,
    resting_hr          INT,
    recovery_status     TEXT,
    weight_kg           DOUBLE PRECISION,
    raw_payload         JSONB,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE activities (
    id                      BIGSERIAL PRIMARY KEY,

    -- identity / provenance
    source                  TEXT NOT NULL,              -- 'intervals_icu' | 'garmin_fit'
    external_id             TEXT NOT NULL,               -- native ID from the source (e.g. 'i123456789', or a FIT file UID)
    superseded_by           BIGINT REFERENCES activities(id),  -- set when a Garmin-fallback row is later matched to an Intervals.icu row
    name                    TEXT,
    type                    TEXT NOT NULL,               -- Ride, Run, VirtualRide, WeightTraining, ...
    sub_type                TEXT,
    device_name             TEXT,
    start_time              TIMESTAMPTZ NOT NULL,
    timezone                TEXT,
    commute                 BOOLEAN,
    race                    BOOLEAN,
    trainer                 BOOLEAN,

    -- duration / distance
    elapsed_time_sec        INT,
    moving_time_sec         INT,
    coasting_time_sec       INT,
    distance_m              DOUBLE PRECISION,
    total_elevation_gain_m  DOUBLE PRECISION,
    total_elevation_loss_m  DOUBLE PRECISION,
    average_speed_mps       DOUBLE PRECISION,
    max_speed_mps           DOUBLE PRECISION,
    pace_sec_per_km         DOUBLE PRECISION,            -- run/swim pace

    -- power
    avg_power_w             DOUBLE PRECISION,
    normalized_power_w      DOUBLE PRECISION,
    intensity_factor        DOUBLE PRECISION,
    variability_index       DOUBLE PRECISION,
    training_load           DOUBLE PRECISION,            -- Intervals.icu's own TSS-equivalent for this activity
    efficiency_factor       DOUBLE PRECISION,
    decoupling_pct          DOUBLE PRECISION,
    ftp_at_time             INT,                         -- FTP setting Intervals.icu used for this activity's calcs
    critical_power_w        INT,
    w_prime_j               INT,
    p_max_w                 INT,
    avg_lr_balance          DOUBLE PRECISION,
    avg_cadence             DOUBLE PRECISION,

    -- heart rate
    avg_hr                  INT,
    max_hr                  INT,
    resting_hr_at_time      INT,
    lthr                    INT,
    trimp                   DOUBLE PRECISION,
    hr_load                 DOUBLE PRECISION,
    hr_load_type            TEXT,

    -- fitness/fatigue snapshot, as Intervals.icu computed it at the time of this activity
    icu_ctl_at_time         DOUBLE PRECISION,
    icu_atl_at_time         DOUBLE PRECISION,

    -- subjective
    feel                    SMALLINT,                    -- 1-5, athlete-logged
    rpe                     SMALLINT,                    -- Intervals.icu icu_rpe

    -- misc
    calories                INT,

    -- everything else (zone-time breakdowns, HRR, interval summaries, rolling
    -- power model, running dynamics, gear, sweet-spot range, etc.) - see
    -- ingest/field_map.py for what's currently written here.
    raw_metrics             JSONB,

    -- full, untouched source payload - insurance against under-scoping fields above.
    raw_payload              JSONB,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (source, external_id)
);

CREATE INDEX idx_activities_start_time ON activities (start_time DESC);
CREATE INDEX idx_activities_type ON activities (type);
CREATE INDEX idx_activities_source ON activities (source);

INSERT INTO athlete_profile (id, ftp, fthr, weight_kg)
VALUES (1, 250, 165, 75)
ON CONFLICT (id) DO NOTHING;
