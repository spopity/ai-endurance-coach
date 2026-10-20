"""
Enables login for the read-only `coach_reader` role (db/migrations/014_readonly_role.sql)
with a fresh random password, and writes READONLY_DATABASE_URL to .env - the connection
the history_analyst agent's SQL tool uses (core/history_query.py).

Run once after the migration, or again to rotate the password:
    python scripts/setup_readonly_role.py
"""
import os
import secrets
import sys
from urllib.parse import urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.db import load_env  # noqa: E402

ENV_PATH = os.path.join(ROOT, ".env")
ROLE = "coach_reader"


def main():
    env = load_env(ENV_PATH)
    password = secrets.token_urlsafe(24)

    conn = psycopg2.connect(env["DATABASE_URL"])
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s;", (ROLE,))
        if not cur.fetchone():
            sys.exit(f"Role '{ROLE}' does not exist - run scripts/create_database.py first (migration 014).")
        cur.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD %s;").format(sql.Identifier(ROLE)), (password,))
    conn.close()

    parts = urlsplit(env["DATABASE_URL"])
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    readonly_url = urlunsplit((parts.scheme, f"{ROLE}:{password}@{host}", parts.path, "", ""))

    with open(ENV_PATH) as f:
        lines = [l for l in f.read().splitlines() if not l.startswith("READONLY_DATABASE_URL=")]
    lines.append(f"READONLY_DATABASE_URL={readonly_url}")
    with open(ENV_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"'{ROLE}' can log in; READONLY_DATABASE_URL written to .env. Restart the services to pick it up.")


if __name__ == "__main__":
    main()
