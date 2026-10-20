"""
Creates the ai-coach-v1 database (athlete_twin_v2) on the existing Postgres
server and applies db/migrations/*.sql in order. Never connects to or
modifies the original athlete_twin database.

Run: python scripts/create_database.py
"""
import os
import sys
import glob
import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def main():
    env = load_env(os.path.join(ROOT, ".env"))
    admin_url = env["ADMIN_DATABASE_URL"]   # points at the 'postgres' maintenance db
    target_url = env["DATABASE_URL"]        # points at athlete_twin_v2
    db_name = target_url.rsplit("/", 1)[-1]

    # 1. Create the database if it doesn't exist yet (must run outside a transaction)
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (db_name,))
        if cur.fetchone():
            print(f"Database '{db_name}' already exists - skipping creation.")
        else:
            cur.execute(f'CREATE DATABASE "{db_name}";')
            print(f"Created database '{db_name}'.")
    conn.close()

    # 2. Apply migrations, in filename order, inside that database
    conn = psycopg2.connect(target_url)
    conn.autocommit = False
    migration_files = sorted(glob.glob(os.path.join(ROOT, "db", "migrations", "*.sql")))
    with conn.cursor() as cur:
        # Track which migrations have run, so this script is safe to re-run
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        conn.commit()

        for path in migration_files:
            fname = os.path.basename(path)
            cur.execute("SELECT 1 FROM schema_migrations WHERE filename = %s;", (fname,))
            if cur.fetchone():
                print(f"  {fname}: already applied, skipping.")
                continue

            with open(path) as f:
                sql = f.read()
            print(f"  {fname}: applying...")
            cur.execute(sql)
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s);", (fname,))
            conn.commit()
            print(f"  {fname}: done.")

    conn.close()
    print("\nSchema is up to date.")

if __name__ == "__main__":
    main()
