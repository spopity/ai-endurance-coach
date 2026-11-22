"""
Nightly encrypted backup: the database (pg_dump, custom format) and state/ (OAuth tokens,
availability, pushed workouts, chat memory), each encrypted with AES-256 (openssl, PBKDF2)
using BACKUP_PASSPHRASE from .env, into /var/backups/ai-coach.

Every dump is checked before it counts: it is decrypted again and `pg_restore --list` has to
read it. Retention: the last 14 daily backups plus one per ISO week for 8 weeks. On failure
Telegram gets a message (--notify).

This container is part of the Proxmox backup, so these files leave the machine with it. The
database lives in another container, so this logical dump is what makes it restorable from here.

Restore (TimescaleDB: into a fresh database, wrapped in pre/post restore):
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BACKUP_PASSPHRASE \\
        -in db-<stamp>.dump.enc -out db.dump
    psql "$ADMIN_DATABASE_URL" -c "CREATE DATABASE athlete_twin_v2"
    psql "$DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()"
    pg_restore --no-owner -d "$DATABASE_URL" db.dump
    psql "$DATABASE_URL" -c "SELECT timescaledb_post_restore()"
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BACKUP_PASSPHRASE \\
        -in state-<stamp>.tar.gz.enc | tar -xz -C /opt/ai-coach-v1

Run:
    python scripts/backup.py [--notify]
"""
import argparse
import logging
import os
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.db import load_env  # noqa: E402
from core.notify import notify_telegram  # noqa: E402
from logging_config import setup_logging  # noqa: E402

logger = logging.getLogger("AI_COACH_V1.backup")

DEST = "/var/backups/ai-coach"
KEEP_DAILY = 14
KEEP_WEEKS = 8
CIPHER = ["-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", "env:BACKUP_PASSPHRASE"]
STATE_SKIP = {"claude-empty"}  # empty work dir


def _env(env: dict) -> dict:
    return {**os.environ, "BACKUP_PASSPHRASE": env["BACKUP_PASSPHRASE"]}


def dump_db(env: dict, out: str):
    with open(out + ".tmp", "wb") as f:
        dump = subprocess.Popen(["pg_dump", "-Fc", "--no-owner", env["DATABASE_URL"]], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)  # TimescaleDB catalog warnings are expected
        enc = subprocess.run(["openssl", "enc", *CIPHER], stdin=dump.stdout, stdout=f, env=_env(env))
        dump.stdout.close()
        if dump.wait() != 0 or enc.returncode != 0:
            raise RuntimeError(f"pg_dump exit {dump.returncode}, openssl exit {enc.returncode}")
    # Verify: decrypt, then pg_restore must read the table of contents and every data block.
    plain = out + ".verify"
    try:
        subprocess.run(["openssl", "enc", "-d", *CIPHER, "-in", out + ".tmp", "-out", plain],
                       env=_env(env), check=True, capture_output=True)
        listing = subprocess.run(["pg_restore", "--list", plain], capture_output=True, text=True)
        full = subprocess.run(["pg_restore", "-f", "/dev/null", plain], capture_output=True, text=True)
    finally:
        if os.path.exists(plain):
            os.remove(plain)
    if listing.returncode or full.returncode or "TABLE DATA public activities" not in listing.stdout:
        raise RuntimeError(f"verification failed: {(listing.stderr or full.stderr)[:200]}")
    os.replace(out + ".tmp", out)


def dump_state(env: dict, out: str):
    tar = subprocess.Popen(["tar", "-cz", "-C", ROOT, *[f"--exclude=state/{s}" for s in STATE_SKIP],
                            "--exclude=*.lock", "--exclude=*.tmp", "state"], stdout=subprocess.PIPE)
    with open(out + ".tmp", "wb") as f:
        enc = subprocess.run(["openssl", "enc", *CIPHER], stdin=tar.stdout, stdout=f, env=_env(env))
    tar.stdout.close()
    if tar.wait() not in (0, 1) or enc.returncode != 0:   # tar 1 = a file changed while read
        raise RuntimeError(f"tar exit {tar.returncode}, openssl exit {enc.returncode}")
    dec = subprocess.Popen(["openssl", "enc", "-d", *CIPHER, "-in", out + ".tmp"], stdout=subprocess.PIPE, env=_env(env))
    with tarfile.open(fileobj=dec.stdout, mode="r|gz") as t:
        names = [m.name for m in t]
    dec.stdout.close()
    if dec.wait() != 0 or not any(n.endswith("availability.json") for n in names):
        raise RuntimeError("state archive verification failed")
    os.replace(out + ".tmp", out)


def prune(now: datetime):
    stamps = sorted({n.split("-", 1)[1].split(".")[0] for n in os.listdir(DEST) if n.startswith(("db-", "state-"))},
                    reverse=True)
    keep, weeks = set(stamps[:KEEP_DAILY]), set()
    for s in stamps:
        d = datetime.strptime(s, "%Y%m%d-%H%M")
        wk = d.isocalendar()[:2]
        if wk not in weeks and now - d <= timedelta(weeks=KEEP_WEEKS):
            weeks.add(wk)
            keep.add(s)
    for n in os.listdir(DEST):
        if n.startswith(("db-", "state-")) and n.split("-", 1)[1].split(".")[0] not in keep:
            os.remove(os.path.join(DEST, n))
            logger.info(f"Pruned {n}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true")
    args = ap.parse_args()
    setup_logging("backup.log")
    env = load_env()
    now = datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M")
    try:
        if not env.get("BACKUP_PASSPHRASE"):
            raise RuntimeError("BACKUP_PASSPHRASE is not set in .env")
        db_out = os.path.join(DEST, f"db-{stamp}.dump.enc")
        dump_db(env, db_out)
        st_out = os.path.join(DEST, f"state-{stamp}.tar.gz.enc")
        dump_state(env, st_out)
        prune(now)
        logger.info(f"Backup OK: {os.path.getsize(db_out) // 1024} KB database, "
                    f"{os.path.getsize(st_out) // 1024} KB state")
    except Exception as e:
        logger.error(f"Backup failed: {e}", exc_info=True)
        for n in os.listdir(DEST):
            if n.endswith((".tmp", ".verify")):
                os.remove(os.path.join(DEST, n))
        if args.notify:
            notify_telegram(env, f"❌ AI coach backup failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
