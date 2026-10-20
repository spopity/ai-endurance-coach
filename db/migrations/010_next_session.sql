-- The head coach's decision for the next training session after a ride/run, made once
-- and shared by the Telegram review, the Strava note and the chat so they can't disagree.
-- Idempotent: safe to re-run.
ALTER TABLE activities ADD COLUMN IF NOT EXISTS next_session text;
ALTER TABLE activities ADD COLUMN IF NOT EXISTS next_session_at timestamptz;
