-- One row per outgoing call to an external service (core/api_log.py): Gemini and Ollama
-- (with tokens and an estimated cost), Intervals.icu, Strava, Telegram, Open-Meteo, Withings.
-- Feeds the dashboard's System page. Idempotent: safe to re-run.
CREATE TABLE IF NOT EXISTS api_calls (
    id               bigserial PRIMARY KEY,
    ts               timestamptz NOT NULL DEFAULT now(),
    service          text NOT NULL,        -- gemini | ollama | intervals | strava | telegram | open_meteo | withings | google
    operation        text,                 -- normalised endpoint ("GET /api/v1/athlete/{id}/activities") or "chat"
    component        text,                 -- which part asked: ai_coach, supervisor, strava_note writer...
    trigger          text,                 -- why: chat, briefing, next_session, strava_note, sync...
    process          text,                 -- app, telegram_bot, post_strava_notes, ...
    model            text,
    status           integer,              -- HTTP status (NULL for LLM calls / network errors)
    ok               boolean NOT NULL DEFAULT true,
    duration_ms      integer,
    input_tokens     integer,
    output_tokens    integer,              -- includes thinking tokens (billed as output)
    thinking_tokens  integer,
    cached_tokens    integer,
    cost_usd         numeric(12, 6),       -- estimate from core/api_log.py PRICES
    error            text
);
CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls (ts DESC);
CREATE INDEX IF NOT EXISTS idx_api_calls_service_ts ON api_calls (service, ts DESC);
