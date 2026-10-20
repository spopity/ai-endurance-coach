-- Changes to the shared next session agreed in chat (core/next_session.py). The coach
-- only ever PROPOSES a change, when the athlete asks for one; it replaces
-- activities.next_session only after the athlete taps Apply in Telegram or the dashboard.
-- Idempotent: safe to re-run.
CREATE TABLE IF NOT EXISTS next_session_proposals (
    id                SERIAL PRIMARY KEY,
    turn_id           TEXT NOT NULL,          -- the chat turn that made it, so that front-end can offer the buttons
    after_activity_id TEXT NOT NULL,          -- activities.external_id it would replace the decision of
    session           TEXT NOT NULL,
    why               TEXT,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'applied', 'discarded')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS next_session_proposals_turn ON next_session_proposals (turn_id);

-- Who set the current decision: 'coach' (decided automatically) or 'athlete' (a chat change they applied).
ALTER TABLE activities ADD COLUMN IF NOT EXISTS next_session_source TEXT;
