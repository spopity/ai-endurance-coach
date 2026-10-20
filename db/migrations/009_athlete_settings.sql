-- Small key/value store for athlete choices made in the dashboard, e.g. the training
-- focus ('cycling' = FTP-first, 'dual' = winter bike + run). Idempotent: safe to re-run.
CREATE TABLE IF NOT EXISTS athlete_settings (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
