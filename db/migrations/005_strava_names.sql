-- Activity names are kept in sync with Strava (scripts/sync_strava_names.py):
-- the Strava activity each row matched, and the name it had before the first
-- rename so the change can be undone. Idempotent: safe to re-run.
ALTER TABLE activities
    ADD COLUMN IF NOT EXISTS strava_id           text,
    ADD COLUMN IF NOT EXISTS name_before_strava  text;

CREATE INDEX IF NOT EXISTS idx_activities_strava_id ON activities (strava_id);
