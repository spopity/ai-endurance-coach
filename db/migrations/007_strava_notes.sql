-- Coach note appended to the Strava description (core/strava_notes.py): the exact
-- text posted, so a re-post can find and replace only our part of the description,
-- and when it was posted (NULL = not yet; also set when skipped via #nocoach).
-- Idempotent: safe to re-run.
ALTER TABLE activities
    ADD COLUMN IF NOT EXISTS strava_note     text,
    ADD COLUMN IF NOT EXISTS strava_note_at  timestamptz;
