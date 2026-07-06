#!/usr/bin/env python3
"""
Supabase / Postgres backup tool.

What it does on every run:
  1. Runs `pg_dump` against the database in DATABASE_URL and gzips the output
     to a timestamped file like  backup-2026-06-30-0200.sql.gz
  2. Uploads that file to Azure Blob Storage.
  3. Deletes blobs in the container older than RETENTION_DAYS.
  4. Treats any of these as a FAILURE: pg_dump errored, upload errored, or the
     gzip file is empty / smaller than MIN_BACKUP_BYTES.
  5. On failure (and optionally on success) sends an email alert (see notify.py).
  6. Records the run to SQLite (backups.db, see db.py) AND appends a line to the
     plain-text backups.log.

All configuration and secrets live in the .env file (see .env.example). Nothing
sensitive is hardcoded. Designed to be run from cron on a Linux VPS.
"""

import gzip
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from azure.storage.blob import BlobServiceClient

import db
import notify
from config import SCRIPT_DIR, get_bool, get_env, get_int, require_env

# Retention can never go below this many days — a guard against a misconfigured
# RETENTION_DAYS wiping recent (or freshly-uploaded) backups. See H2 fix.
RETENTION_FLOOR_DAYS = 7

# Where the gzipped dump and the plain-text run log are written.
BACKUP_DIR = SCRIPT_DIR
LOG_FILE = SCRIPT_DIR / "backups.log"
# Lockfile used to guarantee only one backup runs at a time. See M1 fix.
LOCK_FILE = SCRIPT_DIR / "backup.lock"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def utc_now_iso():
    """UTC timestamp as ISO-8601, used for log lines and DB records."""
    return datetime.now(timezone.utc).isoformat()


def timestamp_for_filename():
    """UTC timestamp for filenames, e.g. 2026-06-30-0200."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")


def human_size(num_bytes):
    """Format a byte count as a short human-readable string."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024


def append_log(status, file_name, size_bytes):
    """Append one line per run: timestamp | status | file | size."""
    line = f"{utc_now_iso()} | {status} | {file_name} | {size_bytes} bytes\n"
    with open(LOG_FILE, "a") as log:
        log.write(line)


def read_log_tail(num_lines=15):
    """Return the last `num_lines` lines of the log (for failure emails)."""
    if not LOG_FILE.exists():
        return ""
    lines = LOG_FILE.read_text().splitlines()
    return "\n".join(lines[-num_lines:])


# --------------------------------------------------------------------------- #
# Step 1: pg_dump + gzip
# --------------------------------------------------------------------------- #

def _split_password(database_url):
    """
    Split a Postgres URL into (url_without_password, password).

    This keeps the password out of the command line — and therefore out of
    `ps` / /proc/<pid>/cmdline — by handing it to pg_dump through the
    PGPASSWORD environment variable instead. Returns (original_url, None) when
    the URL carries no password. Everything else in the URL (user, host, port,
    query params like sslmode) is preserved untouched.
    """
    parts = urlsplit(database_url)
    netloc = parts.netloc
    if "@" not in netloc:
        return database_url, None
    userinfo, hostpart = netloc.rsplit("@", 1)
    if ":" not in userinfo:
        return database_url, None
    user, encoded_password = userinfo.split(":", 1)
    sanitized = urlunsplit(
        (parts.scheme, f"{user}@{hostpart}", parts.path, parts.query, parts.fragment)
    )
    # PGPASSWORD expects the literal password, so undo any URL percent-encoding.
    return sanitized, unquote(encoded_password)


def create_backup(database_url, dest_path, timeout_seconds):
    """
    Dump the whole database and gzip it to `dest_path`.

    We stream pg_dump's stdout straight into a gzip file so the uncompressed
    dump never lives fully in memory or on disk. Three production hardening
    details:
      * The password is passed via the PGPASSWORD environment variable, never on
        the command line, so it can't be read from the process list. (H3)
      * stderr is redirected to a temp file rather than a pipe, so a chatty
        pg_dump can't deadlock by filling an undrained stderr pipe buffer. (H4)
      * A watchdog timer kills pg_dump after `timeout_seconds`, so a hung
        database can never block the job forever. (H5)

    Raises RuntimeError on timeout or non-zero exit, removing any partial file.
    """
    # Keep the password out of argv; pass it through the environment instead.
    sanitized_url, password = _split_password(database_url)
    env = os.environ.copy()
    if password is not None:
        env["PGPASSWORD"] = password

    timed_out = {"flag": False}

    # stderr goes to a real temp file (not a PIPE) so it can never fill up and
    # deadlock against us while we're busy streaming stdout.
    with tempfile.TemporaryFile() as err_file, gzip.open(dest_path, "wb") as gz_out:
        proc = subprocess.Popen(
            ["pg_dump", "--dbname", sanitized_url, "--no-owner", "--no-privileges"],
            stdout=subprocess.PIPE,
            stderr=err_file,
            env=env,
        )

        # Watchdog: if pg_dump overruns the timeout, kill it. Killing closes its
        # stdout, which unblocks the copy below even if pg_dump was hung
        # producing no output at all.
        def _kill_on_timeout():
            timed_out["flag"] = True
            proc.kill()

        watchdog = threading.Timer(timeout_seconds, _kill_on_timeout)
        watchdog.start()
        try:
            shutil.copyfileobj(proc.stdout, gz_out)
            proc.stdout.close()
            proc.wait()
        finally:
            watchdog.cancel()

        err_file.seek(0)
        stderr_text = err_file.read().decode("utf-8", "replace").strip()

    if timed_out["flag"]:
        dest_path.unlink(missing_ok=True)  # never keep/upload a partial dump
        raise RuntimeError(f"pg_dump timed out after {timeout_seconds}s and was killed")

    if proc.returncode != 0:
        dest_path.unlink(missing_ok=True)  # never keep/upload a partial dump
        raise RuntimeError(
            f"pg_dump failed (exit {proc.returncode}): {stderr_text or '(no stderr)'}"
        )


# --------------------------------------------------------------------------- #
# Steps 2 + 3: Azure Blob upload and retention cleanup
# --------------------------------------------------------------------------- #

def get_container_client(connection_string, container_name):
    """Return an Azure container client, creating the container if needed."""
    service = BlobServiceClient.from_connection_string(connection_string)
    container = service.get_container_client(container_name)
    if not container.exists():
        container.create_container()
    return container


def upload_backup(container, file_path):
    """Upload a single backup file to the container under its own name."""
    with open(file_path, "rb") as data:
        container.upload_blob(name=file_path.name, data=data, overwrite=True)


def delete_old_backups(container, retention_days):
    """
    Delete blobs older than `retention_days`. Returns the deleted names.

    Cleanup errors propagate to the caller: stale backups silently piling up is
    a real problem, so a failed cleanup counts as a failed run.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = []
    for blob in container.list_blobs():
        # Only ever touch our own backup files. Anything else in the container
        # (however old) is left strictly alone. See H1 fix.
        if not (blob.name.startswith("backup-") and blob.name.endswith(".sql.gz")):
            continue
        if blob.last_modified and blob.last_modified < cutoff:
            container.delete_blob(blob.name)
            deleted.append(blob.name)
    return deleted


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def resolve_retention_days():
    """
    Return a safe RETENTION_DAYS value.

    Refuses to prune too aggressively: anything below RETENTION_FLOOR_DAYS, or a
    non-numeric value, is clamped up to the floor with a clear warning. A blank
    value uses the normal 90-day default. This guarantees 0 / negative / typo'd
    values can never delete recent (or just-uploaded) backups. See H2 fix.
    """
    raw = get_env("RETENTION_DAYS")
    if raw is None or raw == "":
        return 90  # normal default when unset

    try:
        days = int(raw)
    except ValueError:
        print(
            f"WARNING: RETENTION_DAYS={raw!r} is not a number; "
            f"using safe floor of {RETENTION_FLOOR_DAYS} days.",
            file=sys.stderr,
        )
        return RETENTION_FLOOR_DAYS

    if days < RETENTION_FLOOR_DAYS:
        print(
            f"WARNING: RETENTION_DAYS={days} is below the minimum; "
            f"using safe floor of {RETENTION_FLOOR_DAYS} days.",
            file=sys.stderr,
        )
        return RETENTION_FLOOR_DAYS

    return days


def record_run(status, timestamp, file_name, size_bytes, duration, error=None):
    """Persist a run to BOTH the plain-text log and the SQLite history."""
    log_status = "SUCCESS" if status == db.STATUS_SUCCESS else "FAILURE"
    append_log(log_status, file_name, size_bytes)
    db.insert_run(
        db.BackupRun(
            timestamp=timestamp,
            status=status,
            file_name=file_name,
            file_size_bytes=size_bytes,
            duration_seconds=round(duration, 2),
            error_message=error,
        )
    )


def acquire_lock():
    """
    Take an exclusive, non-blocking lock so two backups never run at once.

    Returns the open lock-file handle on success (keep it open to hold the
    lock; the OS releases it automatically when the handle is closed or the
    process exits). Returns None if another run already holds the lock, so the
    caller can exit cleanly instead of starting a second pg_dump. See M1 fix.

    `fcntl` is Unix-only, so it's imported here (not at module load) — that keeps
    the script importable on platforms without it (e.g. Windows). Where it's
    missing we can't lock, so we warn and proceed WITHOUT overlap protection. On
    Linux (the VM) locking works exactly as before.
    """
    handle = open(LOCK_FILE, "w")
    try:
        import fcntl
    except ImportError:
        print(
            "WARNING: file locking is unavailable on this platform; running "
            "the backup without overlap protection.",
            file=sys.stderr,
        )
        return handle
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def prune_local_backups(keep):
    """
    Keep only the newest `keep` local backup files; delete the older ones.

    Called only after a confirmed upload, so a deleted file is always safely in
    Azure already. `keep` comes from BACKUP_KEEP_LOCAL (default 1); 0 means keep
    none locally. This stops local gzips accumulating forever. See M2 fix.
    """
    keep = max(0, keep)
    local_backups = sorted(
        BACKUP_DIR.glob("backup-*.sql.gz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,  # newest first
    )
    for old in local_backups[keep:]:
        old.unlink(missing_ok=True)


def main():
    """Take the single-run lock, then perform exactly one backup."""
    lock_handle = acquire_lock()
    if lock_handle is None:
        print(
            "Another backup is already running; exiting without starting a second one.",
            file=sys.stderr,
        )
        return 0
    try:
        return _run_backup_once()
    finally:
        # Closing the handle releases the flock. See M1 fix.
        lock_handle.close()


def _run_backup_once():
    notify_on_success = get_bool("NOTIFY_ON_SUCCESS", False)

    started_at = time.monotonic()
    timestamp = utc_now_iso()
    file_name = f"backup-{timestamp_for_filename()}.sql.gz"
    dest_path = BACKUP_DIR / file_name
    size_bytes = 0
    dump_ok = False  # True once we have a complete, validated local dump

    try:
        # Required secrets/config — a missing one fails loud immediately.
        database_url = require_env("DATABASE_URL")
        azure_conn = require_env("AZURE_STORAGE_CONNECTION_STRING")
        container_name = require_env("AZURE_CONTAINER_NAME")
        min_bytes = get_int("MIN_BACKUP_BYTES", 1024)
        retention_days = resolve_retention_days()
        # Max seconds pg_dump may run before it's killed (default 1 hour).
        pg_dump_timeout = get_int("PG_DUMP_TIMEOUT", 3600)

        # 1. Dump + gzip.
        create_backup(database_url, dest_path, pg_dump_timeout)
        size_bytes = dest_path.stat().st_size

        # 4. Reject empty / suspiciously small dumps BEFORE uploading them.
        if size_bytes < min_bytes:
            raise RuntimeError(
                f"backup file is suspiciously small: {size_bytes} bytes "
                f"(minimum {min_bytes}). Treating as failure."
            )
        dump_ok = True  # complete, validated dump exists on disk from here on

        # 2. Upload, then 3. prune old remote backups.
        container = get_container_client(azure_conn, container_name)
        upload_backup(container, dest_path)
        delete_old_backups(container, retention_days)

        # 4. Upload confirmed — now trim local copies so they don't pile up.
        prune_local_backups(get_int("BACKUP_KEEP_LOCAL", 1))

    except Exception as exc:  # noqa: BLE001 - catch everything to report it
        # Clean up a partial/incomplete local dump (e.g. disk full mid-dump).
        # But if the dump itself completed and only a later step (upload/prune)
        # failed, KEEP the good local file — it may be the only copy. See M2.
        if not dump_ok:
            dest_path.unlink(missing_ok=True)
        duration = time.monotonic() - started_at
        record_run(db.STATUS_FAIL, timestamp, file_name, size_bytes, duration, str(exc))
        # Email must never crash the backup — notify.py swallows its own errors.
        notify.send_failure_alert(str(exc), timestamp, read_log_tail())
        print(f"FAILURE: {exc}", file=sys.stderr)
        return 1

    # ---- SUCCESS ----
    duration = time.monotonic() - started_at
    record_run(db.STATUS_SUCCESS, timestamp, file_name, size_bytes, duration)
    if notify_on_success:
        notify.send_success_alert(file_name, size_bytes, timestamp)
    # Dead-man's-switch: ping the heartbeat only after a fully successful run,
    # so an external monitor can detect backups that silently stop. See H6 fix.
    heartbeat_url = get_env("HEARTBEAT_URL")
    if heartbeat_url:
        notify.ping_heartbeat(heartbeat_url)
    print(f"SUCCESS: {file_name} ({human_size(size_bytes)}) in {duration:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
