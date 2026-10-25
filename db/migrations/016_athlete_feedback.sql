-- The athlete's own feedback, asked by the Telegram bot (core/feedback.py). Kept out of
-- activities.rpe/feel and daily_metrics.soreness/motivation on purpose: those mirror
-- Intervals.icu and are overwritten by every sync.

-- One row per activity the bot asked about, created when the question is sent (so it is
-- asked only once); rpe/legs stay NULL until answered.
CREATE TABLE IF NOT EXISTS session_feedback (
    external_id   TEXT PRIMARY KEY,               -- activities.external_id
    asked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    rpe           SMALLINT CHECK (rpe BETWEEN 1 AND 10),
    legs          TEXT CHECK (legs IN ('fresh', 'normal', 'heavy', 'sore')),
    answered_at   TIMESTAMPTZ
);

-- Morning check-in after the briefing, one row per local day.
CREATE TABLE IF NOT EXISTS daily_checkin (
    day           DATE PRIMARY KEY,
    asked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    body          TEXT CHECK (body IN ('fresh', 'normal', 'sore', 'very_sore', 'ill')),
    motivation    TEXT CHECK (motivation IN ('high', 'normal', 'low')),
    answered_at   TIMESTAMPTZ
);

GRANT SELECT ON session_feedback, daily_checkin TO coach_reader;
