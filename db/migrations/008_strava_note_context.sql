-- Extra context the athlete gave for an activity's Strava coach note (Telegram /strava
-- or the dashboard), kept so a later re-post still uses it. Idempotent: safe to re-run.
ALTER TABLE activities ADD COLUMN IF NOT EXISTS strava_note_context text;
