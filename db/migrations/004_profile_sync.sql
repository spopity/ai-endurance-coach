-- Athlete profile synced from Intervals.icu (sport settings for Ride + athlete
-- weight/resting HR), plus the latest Intervals.icu eFTP so drift between the
-- set FTP and estimated FTP can be flagged. Idempotent: safe to re-run.
ALTER TABLE athlete_profile
    ADD COLUMN IF NOT EXISTS max_hr       integer,
    ADD COLUMN IF NOT EXISTS resting_hr   integer,
    ADD COLUMN IF NOT EXISTS eftp         double precision,
    ADD COLUMN IF NOT EXISTS w_prime_j    double precision,
    ADD COLUMN IF NOT EXISTS p_max_w      double precision,
    ADD COLUMN IF NOT EXISTS eftp_day     date,
    ADD COLUMN IF NOT EXISTS source       text DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS synced_at    timestamptz;
