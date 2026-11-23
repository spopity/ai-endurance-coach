-- Richer morning check-in (core/feedback.py): how the night felt and how stressful life is right
-- now (work, family), next to body and motivation - a short Hooper-style well-being check. "off"
-- (a bit off: throat, headache, run down) is the athlete's half of the illness early warning
-- (core/illness.py reads the night's numbers).
ALTER TABLE daily_checkin DROP CONSTRAINT IF EXISTS daily_checkin_body_check;
ALTER TABLE daily_checkin ADD CONSTRAINT daily_checkin_body_check
    CHECK (body IN ('fresh', 'normal', 'sore', 'very_sore', 'off', 'ill'));
ALTER TABLE daily_checkin
    ADD COLUMN IF NOT EXISTS sleep  TEXT CHECK (sleep IN ('good', 'ok', 'poor')),
    ADD COLUMN IF NOT EXISTS stress TEXT CHECK (stress IN ('low', 'normal', 'high'));
