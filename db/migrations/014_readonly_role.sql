-- Read-only role for the history_analyst agent's free-form SQL tool (core/history_query.py).
-- The LLM writes its own SELECTs, so they must never run as the app's superuser: this role
-- can only read the athlete-data tables below. Created NOLOGIN without a password (no
-- secrets in migrations) - scripts/setup_readonly_role.py enables login, sets a random
-- password and writes READONLY_DATABASE_URL to .env.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'coach_reader') THEN
        CREATE ROLE coach_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO coach_reader', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO coach_reader;
GRANT SELECT ON activities, daily_metrics, training_blocks, athlete_profile TO coach_reader;

-- Belt and braces: even a grant mistake can't turn a query into a write or a runaway scan.
ALTER ROLE coach_reader SET default_transaction_read_only = on;
ALTER ROLE coach_reader SET statement_timeout = '15s';
