"""
Nightly encrypted backup: the database (pg_dump, custom format) and state/ (OAuth tokens,
availability, pushed workouts, chat memory), each encrypted with AES-256 (openssl, PBKDF2)
using BACKUP_PASSPHRASE from .env, into /var/backups/ai-coach.

Every dump is checked before it counts: it is decrypted again and `pg_restore --list` has to
read it. Retention: the last 14 daily backups plus one per ISO week for 8 weeks. On failure
Telegram gets a message (--notify).

Off-machine copy: the folder is then mirrored to the NAS over SMB (smbprotocol, no mount and no
system packages): NAS_HOST, NAS_SHARE, NAS_USER, NAS_PASSWORD in .env, into <share>/ai-coach/.
SMB 3 encryption is used when the NAS offers it; an SMB 2-only NAS gets a signed session (the
files are AES-encrypted anyway) and a warning in the log. New files are uploaded under a temporary name, read back and compared by SHA-256, then renamed;
files pruned locally are removed there too, so the NAS keeps the same 14 daily + 8 weekly set.
The files are already encrypted, so the NAS never sees plain data. The last successful copy is
recorded in state/backup_nas.json (the hourly health check alerts when it gets old). Without
NAS_PASSWORD the copy is skipped with a warning.

The database lives in another container, so this logical dump is what makes it restorable from
here.

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
    python scripts/backup.py [--notify]     # backup + NAS copy
    python scripts/backup.py --nas-only     # only (re)mirror the existing files to the NAS
"""
import argparse
import hashlib
import json
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
logging.getLogger("smbprotocol").setLevel(logging.WARNING)
logging.getLogger("smbclient").setLevel(logging.WARNING)

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


NAS_DIR = "ai-coach"
NAS_STATE = os.path.join(ROOT, "state", "backup_nas.json")
BACKUP_FILE = ("db-", "state-")


def _sha256(read) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


def copy_to_nas(env: dict) -> dict:
    """Mirrors DEST to <NAS_SHARE>/ai-coach on NAS_HOST; returns {uploaded, removed, kept}."""
    import smbclient
    from smbprotocol.exceptions import SMBException
    host, share = env["NAS_HOST"], env["NAS_SHARE"]
    try:
        smbclient.register_session(host, username=env["NAS_USER"], password=env["NAS_PASSWORD"],
                                   encrypt=True, connection_timeout=30)
    except SMBException as e:
        if "encryption" not in str(e).lower():
            raise
        # The NAS speaks only SMB 2 (no transport encryption). The files are AES-encrypted
        # already and the session stays signed (tamper-proof), so carry on - but say so.
        logger.warning("The NAS doesn't offer SMB 3 encryption - copying over a signed SMB 2 session. "
                       "Raise the NAS's maximum SMB protocol to SMB3 to encrypt the transfer too.")
        smbclient.reset_connection_cache()
        smbclient.register_session(host, username=env["NAS_USER"], password=env["NAS_PASSWORD"],
                                   encrypt=False, require_signing=True, connection_timeout=30)
    remote = rf"\\{host}\{share}\{NAS_DIR}"
    smbclient.makedirs(remote, exist_ok=True)
    local = {n for n in os.listdir(DEST) if n.startswith(BACKUP_FILE) and n.endswith(".enc")}
    there = {e.name: e.stat().st_size for e in smbclient.scandir(remote)}
    uploaded, removed = [], []
    for n in sorted(local):
        path = os.path.join(DEST, n)
        if there.get(n) == os.path.getsize(path):
            continue
        tmp = rf"{remote}\{n}.part"
        with open(path, "rb") as src, smbclient.open_file(tmp, mode="wb") as dst:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                dst.write(chunk)
        with open(path, "rb") as f:
            want = _sha256(f.read)
        with smbclient.open_file(tmp, mode="rb") as f:
            got = _sha256(f.read)
        if got != want:
            smbclient.remove(tmp)
            raise RuntimeError(f"NAS copy of {n} doesn't match (checksum)")
        if n in there:
            smbclient.remove(rf"{remote}\{n}")
        smbclient.rename(tmp, rf"{remote}\{n}")
        uploaded.append(n)
    for n in there:
        if (n.startswith(BACKUP_FILE) and n.endswith(".enc") and n not in local) or n.endswith(".part"):
            smbclient.remove(rf"{remote}\{n}")
            removed.append(n)
    smbclient.reset_connection_cache()
    result = {"uploaded": uploaded, "removed": removed, "kept": len(local)}
    with open(NAS_STATE + ".tmp", "w") as f:
        json.dump({"last_ok": datetime.now().isoformat(timespec="seconds"), **result}, f)
    os.replace(NAS_STATE + ".tmp", NAS_STATE)
    return result


def nas_step(env: dict, notify: bool) -> bool:
    if not env.get("NAS_PASSWORD"):
        logger.warning("NAS_PASSWORD not set - backup stays on this machine only.")
        return True
    try:
        r = copy_to_nas(env)
        logger.info(f"NAS copy OK: {len(r['uploaded'])} uploaded, {len(r['removed'])} removed, {r['kept']} files there")
        return True
    except Exception as e:
        logger.error(f"NAS copy failed: {e}", exc_info=True)
        if notify:
            notify_telegram(env, f"❌ AI coach backup: copy to the NAS failed: {e}\nThe local backup is fine.")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--nas-only", action="store_true", help="only mirror the existing backups to the NAS")
    args = ap.parse_args()
    setup_logging("backup.log")
    env = load_env()
    if args.nas_only:
        sys.exit(0 if nas_step(env, args.notify) else 1)
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
    if not nas_step(env, args.notify):
        sys.exit(1)


if __name__ == "__main__":
    main()
