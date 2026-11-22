-- Gym lifts logged by the athlete (Telegram /gym, core/gym_log.py): one row per exercise of a
-- session, so the coach can make the gym progressive (next weight from the last one).
CREATE TABLE IF NOT EXISTS gym_sets (
    id            BIGSERIAL PRIMARY KEY,
    day           DATE NOT NULL,                  -- local day of the session
    external_id   TEXT,                           -- activities.external_id of that day's gym session, if any
    exercise      TEXT NOT NULL,                  -- normalised name, e.g. 'squat'
    weight_kg     NUMERIC(6, 2),                  -- NULL = bodyweight
    reps          SMALLINT NOT NULL CHECK (reps BETWEEN 1 AND 100),
    sets          SMALLINT NOT NULL DEFAULT 1 CHECK (sets BETWEEN 1 AND 20),
    logged_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS gym_sets_exercise_day ON gym_sets (exercise, day DESC);

GRANT SELECT ON gym_sets TO coach_reader;
