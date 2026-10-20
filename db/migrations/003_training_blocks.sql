-- Rolling periodization plan. Since there's no specific target event right
-- now, this holds a repeating macrocycle (3 weeks Build + 1 week Recovery)
-- rather than a Base/Build/Peak/Taper plan counting toward a race date.
-- One row per calendar week (Monday-Sunday) so ai_coach can look up "today's
-- block" directly, while still grouping into multi-week phases conceptually.

CREATE TABLE training_blocks (
    id                          SERIAL PRIMARY KEY,
    phase                       TEXT NOT NULL,              -- 'Build' | 'Recovery'
    start_date                  DATE NOT NULL,
    end_date                    DATE NOT NULL,
    target_weekly_load_min      DOUBLE PRECISION,
    target_weekly_load_max      DOUBLE PRECISION,
    primary_focus               TEXT NOT NULL,
    is_deload                   BOOLEAN NOT NULL DEFAULT FALSE,
    notes                       TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_training_blocks_dates ON training_blocks (start_date, end_date);
