#!/usr/bin/env python3
"""
Supabase Storage (uploaded files) backup tool.

This is the companion to backup.py. Where backup.py dumps the Postgres
DATABASE, this module backs up the FILES your users have uploaded to Supabase
Storage buckets. It is deliberately a SEPARATE, independently runnable script so
it can be tested on its own before being added to any schedule.

What it does on every run:
  1. Connects to Supabase using SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.
  2. Decides which buckets to back up: every bucket, or just the comma-separated
     list in BACKUP_BUCKETS.
  3. Downloads every file from those buckets into a temporary directory,
     preserving the bucket + folder structure. It pages through big buckets and
     skips (with a logged warning) any single file that fails, so one bad object
     never aborts the whole run.
  4. Packs everything into ONE archive: storage-backup-YYYY-MM-DD-HHMM.tar.gz
     (same timestamp style as the database backups).
  5. Uploads that archive to the SAME Azure Blob container as the DB backups,
     then prunes only old storage-backup-*.tar.gz blobs (never DB backups).
  6. Records the run in the shared SQLite history (db.py) tagged as "storage",
     and appends a line to backups.log.
  7. Cleans up the temp directory and (respecting BACKUP_KEEP_LOCAL) the local
     archive after a confirmed upload.

STRICTLY READ-ONLY on Supabase: it only LISTS and DOWNLOADS. It never deletes,
overwrites, or changes anything in Supabase Storage.

All configuration and secrets live in the .env file (see .env.example). Nothing
sensitive is hardcoded. Run it manually with:  python storage_backup.py
"""

import argparse
import shutil
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import db
import notify
from config import SCRIPT_DIR, get_bool, get_env, get_int, require_env

# Reuse the database backup's Azure + timestamp + logging helpers so there is
# exactly ONE implementation of "talk to our Azure container", "format a
# timestamp", and "append to backups.log". See backup.py.
from backup import (
    append_log,
    get_container_client,
    human_size,
    read_log_tail,
    resolve_retention_days,
    timestamp_for_filename,
    upload_backup,
    utc_now_iso,
)

# The archive and plain-text log live alongside the project files, exactly like
# the DB backups.
BACKUP_DIR = SCRIPT_DIR
# Our own lock file, separate from backup.py's, so a storage backup and a DB
# backup CAN run at the same time, but two storage backups can never overlap.
LOCK_FILE = SCRIPT_DIR / "storage_backup.lock"

# Supabase inserts this zero-byte placeholder to keep an "empty" folder visible.
# It is not real user data, so we skip it.
EMPTY_FOLDER_PLACEHOLDER = ".emptyFolderPlaceholder"

# How many entries to request per Storage "list" call when paging a big bucket.
PAGE_SIZE = 100


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _field(entry, key):
    """
    Read a field from a Supabase Storage result item.

    The client returns buckets/files as either dicts or small objects depending
    on its version, so this reads `key` whichever way the item is shaped.
    """
    if isinstance(entry, dict):
        return entry.get(key)
    return getattr(entry, key, None)


def _file_size(entry):
    """Best-effort file size in bytes from a Storage list entry (None if absent)."""
    metadata = _field(entry, "metadata")
    if isinstance(metadata, dict):
        return metadata.get("size")
    return None


# --------------------------------------------------------------------------- #
# Step 1: connect to Supabase (read-only use only)
# --------------------------------------------------------------------------- #

def get_supabase_client():
    """
    Build a Supabase client from .env. Fails loud if config or the lib is missing.

    Needs the SERVICE-ROLE key (not the anon key) so it can read every bucket,
    including private ones. We only ever call list/download with it.
    """
    url = require_env("SUPABASE_URL")
    service_role_key = require_env("SUPABASE_SERVICE_ROLE_KEY")
    try:
        from supabase import create_client
    except ImportError as exc:
        raise RuntimeError(
            "The 'supabase' package is not installed. "
            "Run 'pip install -r requirements.txt' first."
        ) from exc
    return create_client(url, service_role_key)


# --------------------------------------------------------------------------- #
# Step 2: choose which buckets to back up
# --------------------------------------------------------------------------- #

def list_target_buckets(client):
    """
    Return the list of bucket names to back up.

    BACKUP_BUCKETS empty/unset -> every bucket in the project.
    BACKUP_BUCKETS set         -> just those names (comma-separated), after
                                  dropping any that don't exist (with a warning).
    Fails loud if a specific list was given but none of those buckets exist.
    """
    existing = [_field(b, "name") for b in client.storage.list_buckets()]
    existing = [name for name in existing if name]

    requested_raw = get_env("BACKUP_BUCKETS", "") or ""
    requested = [name.strip() for name in requested_raw.split(",") if name.strip()]

    if not requested:
        return existing

    targets = []
    for name in requested:
        if name in existing:
            targets.append(name)
        else:
            print(
                f"WARNING: bucket {name!r} from BACKUP_BUCKETS was not found; skipping.",
                file=sys.stderr,
            )
    if not targets:
        raise RuntimeError(
            "None of the buckets listed in BACKUP_BUCKETS exist: " + requested_raw
        )
    return targets


# --------------------------------------------------------------------------- #
# Step 3: list + download files (paginated, recursive, read-only)
# --------------------------------------------------------------------------- #

def list_bucket_files(client, bucket, prefix=""):
    """
    Return every file inside `bucket` (recursively) as (path, size_bytes) pairs.

    `path` is relative to the bucket; `size_bytes` comes from the file's metadata
    (None if Supabase didn't report it). Supabase Storage lists one "folder" at a
    time and returns at most a page of entries, so we page with limit/offset and
    recurse into sub-folders. A folder is an entry whose `id` is None; anything
    else is a real file. Used by both the dry-run preview and the real download.
    """
    api = client.storage.from_(bucket)
    files = []
    offset = 0
    while True:
        batch = api.list(path=prefix, options={"limit": PAGE_SIZE, "offset": offset})
        if not batch:
            break
        for entry in batch:
            name = _field(entry, "name")
            if not name or name == EMPTY_FOLDER_PLACEHOLDER:
                continue
            full_path = f"{prefix}/{name}" if prefix else name
            if _field(entry, "id") is None:
                # A sub-folder: recurse into it.
                files.extend(list_bucket_files(client, bucket, full_path))
            else:
                files.append((full_path, _file_size(entry)))
        # A short page means we've reached the end of this folder.
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return files


def download_bucket(client, bucket, dest_root):
    """
    Download every file in `bucket` into dest_root/<bucket>/..., keeping folders.

    Returns (downloaded_count, failed_count). A single file that fails to
    download is logged and skipped so it can never abort the whole backup.
    """
    api = client.storage.from_(bucket)
    file_entries = list_bucket_files(client, bucket)
    downloaded = 0
    failed = 0
    for rel_path, _size in file_entries:
        dest = dest_root / bucket / rel_path
        try:
            data = api.download(rel_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            downloaded += 1
        except Exception as exc:  # noqa: BLE001 - skip one bad file, keep going
            failed += 1
            print(f"WARNING: skipped {bucket}/{rel_path}: {exc}", file=sys.stderr)
    return downloaded, failed


# --------------------------------------------------------------------------- #
# Step 4: package into one tar.gz
# --------------------------------------------------------------------------- #

def make_archive(source_dir, archive_path):
    """
    Pack everything under `source_dir` into a single gzipped tar at archive_path.

    Each bucket directory becomes a top-level entry in the archive, so the tree
    inside the archive is  <bucket>/<folder>/<file>.
    """
    with tarfile.open(archive_path, "w:gz") as tar:
        for child in sorted(source_dir.iterdir()):
            tar.add(child, arcname=child.name)


# --------------------------------------------------------------------------- #
# Step 5: Azure upload + storage-only retention
# --------------------------------------------------------------------------- #

def delete_old_storage_backups(container, retention_days):
    """
    Delete only storage-backup-*.tar.gz blobs older than `retention_days`.

    The prefix/suffix filter means this can NEVER touch the database backups
    (backup-*.sql.gz) or anything else in the shared container. Returns the
    deleted names.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = []
    for blob in container.list_blobs():
        if not (blob.name.startswith("storage-backup-") and blob.name.endswith(".tar.gz")):
            continue
        if blob.last_modified and blob.last_modified < cutoff:
            container.delete_blob(blob.name)
            deleted.append(blob.name)
    return deleted


def prune_local_storage_archives(keep):
    """
    Keep only the newest `keep` local storage archives; delete the older ones.

    Mirrors backup.py's local pruning but matches ONLY storage-backup-*.tar.gz,
    so DB backups on disk are left strictly alone. `keep` comes from
    BACKUP_KEEP_LOCAL (default 1); 0 keeps none locally.
    """
    keep = max(0, keep)
    archives = sorted(
        BACKUP_DIR.glob("storage-backup-*.tar.gz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,  # newest first
    )
    for old in archives[keep:]:
        old.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Step 6: record the run
# --------------------------------------------------------------------------- #

def record_run(status, timestamp, file_name, size_bytes, duration, error=None):
    """Persist a storage run to BOTH backups.log and the SQLite history."""
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
            backup_type=db.TYPE_STORAGE,
        )
    )


# --------------------------------------------------------------------------- #
# Dry run: preview only, download/upload/record NOTHING
# --------------------------------------------------------------------------- #

def run_dry_run():
    """
    Preview what a real run WOULD back up, without changing anything.

    Connects to Supabase (needs SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY) and,
    for each target bucket, prints the file count and total size. It downloads
    nothing, creates no archive, uploads nothing, and writes no history row. It
    does not even need the Azure config. Returns a process exit code.
    """
    print("DRY RUN — nothing will be downloaded, uploaded, or recorded.")
    try:
        print("Connecting to Supabase...")
        client = get_supabase_client()
        print("Listing buckets...")
        buckets = list_target_buckets(client)
    except Exception as exc:  # noqa: BLE001 - report clearly, no traceback
        print(f"FAILURE: {exc}", file=sys.stderr)
        return 1

    print(f"Would back up {len(buckets)} bucket(s).")
    grand_files = 0
    grand_size = 0
    for bucket in buckets:
        entries = list_bucket_files(client, bucket)
        count = len(entries)
        size = sum((s or 0) for _, s in entries)
        grand_files += count
        grand_size += size
        print(f"  {bucket}: {count} file(s), {human_size(size)}")

    print(
        f"TOTAL: {grand_files} file(s), {human_size(grand_size)} "
        f"across {len(buckets)} bucket(s)."
    )
    if grand_files == 0:
        print("Note: nothing to back up yet (no buckets, or all buckets are empty).")
    print("Dry run complete. No changes were made anywhere.")
    return 0


# --------------------------------------------------------------------------- #
# Single-run lock + orchestration
# --------------------------------------------------------------------------- #

def acquire_lock():
    """
    Take an exclusive, non-blocking lock so two storage backups never overlap.

    Returns the open handle on success (keep it open to hold the lock; the OS
    releases it when the handle closes or the process exits), or None if another
    storage backup already holds it. Uses its own file so it does not block the
    database backup. Mirrors backup.py's lock.

    `fcntl` is Unix-only, so it's imported here (not at module load) — that keeps
    the script importable and runnable on platforms without it (e.g. Windows).
    Where it's missing we can't lock, so we warn and proceed WITHOUT overlap
    protection. On Linux (the VM) locking works exactly as before.
    """
    handle = open(LOCK_FILE, "w")
    try:
        import fcntl
    except ImportError:
        print(
            "WARNING: file locking is unavailable on this platform; running "
            "the storage backup without overlap protection.",
            file=sys.stderr,
        )
        return handle
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main(argv=None):
    """Parse args, then either preview (--dry-run) or run one storage backup."""
    parser = argparse.ArgumentParser(
        description="Back up Supabase Storage buckets (files) to Azure Blob Storage."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only: list buckets and count files/size, but download, "
        "archive, upload, and record NOTHING. Pure read-only.",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        return run_dry_run()

    # A real run takes the single-run lock so two storage backups never overlap.
    lock_handle = acquire_lock()
    if lock_handle is None:
        print(
            "Another storage backup is already running; "
            "exiting without starting a second one.",
            file=sys.stderr,
        )
        return 0
    try:
        return _run_storage_backup_once()
    finally:
        # Closing the handle releases the flock.
        lock_handle.close()


def _run_storage_backup_once():
    started_at = time.monotonic()
    timestamp = utc_now_iso()
    file_name = f"storage-backup-{timestamp_for_filename()}.tar.gz"
    archive_path = BACKUP_DIR / file_name
    size_bytes = 0
    total_downloaded = 0
    total_failed = 0
    # Temp dir for the downloaded files; always removed in `finally`.
    temp_dir = Path(tempfile.mkdtemp(prefix="storage-backup-"))
    archive_ok = False  # True once a complete archive exists on disk

    try:
        # Required config — a missing value fails loud immediately.
        azure_conn = require_env("AZURE_STORAGE_CONNECTION_STRING")
        container_name = require_env("AZURE_CONTAINER_NAME")
        retention_days = resolve_retention_days()

        # 1 + 2. Connect and decide which buckets to back up.
        print("Connecting to Supabase...")
        client = get_supabase_client()
        print("Listing buckets...")
        buckets = list_target_buckets(client)
        print(f"Backing up {len(buckets)} bucket(s): {', '.join(buckets) or '(none)'}")

        # 3. Download every file from each bucket (read-only).
        for bucket in buckets:
            print(f"Downloading files from '{bucket}'...")
            downloaded, failed = download_bucket(client, bucket, temp_dir)
            total_downloaded += downloaded
            total_failed += failed
            print(f"  {bucket}: {downloaded} downloaded, {failed} skipped")

        # If files existed but not a single one downloaded, the backup would be
        # empty and useless — treat that as a failure rather than uploading it.
        if total_downloaded == 0 and total_failed > 0:
            raise RuntimeError(
                f"all {total_failed} file(s) failed to download; nothing to archive"
            )

        # 4. Package everything into one archive.
        print(f"Archiving {total_downloaded} file(s) into {file_name}...")
        make_archive(temp_dir, archive_path)
        size_bytes = archive_path.stat().st_size
        archive_ok = True

        # 5. Upload, then prune only old storage archives (never DB backups).
        print(f"Uploading {human_size(size_bytes)} to Azure container '{container_name}'...")
        container = get_container_client(azure_conn, container_name)
        upload_backup(container, archive_path)
        print("Applying retention (storage archives only)...")
        deleted = delete_old_storage_backups(container, retention_days)
        if deleted:
            print(f"  pruned {len(deleted)} old storage archive(s) from Azure")

        # 7. Upload confirmed — trim local archives per BACKUP_KEEP_LOCAL.
        print("Cleaning up local files...")
        prune_local_storage_archives(get_int("BACKUP_KEEP_LOCAL", 1))

    except Exception as exc:  # noqa: BLE001 - catch everything to report it
        # Drop a partial archive; a half-written tar is worse than none.
        if not archive_ok:
            archive_path.unlink(missing_ok=True)
        duration = time.monotonic() - started_at
        record_run(db.STATUS_FAIL, timestamp, file_name, size_bytes, duration, str(exc))
        # Email must never crash the backup — notify.py swallows its own errors.
        notify.send_failure_alert(str(exc), timestamp, read_log_tail())
        print(f"FAILURE: {exc}", file=sys.stderr)
        return 1
    finally:
        # The downloaded files are only an intermediate step — always clean up.
        shutil.rmtree(temp_dir, ignore_errors=True)

    # ---- SUCCESS ----
    duration = time.monotonic() - started_at
    record_run(db.STATUS_SUCCESS, timestamp, file_name, size_bytes, duration)
    if total_downloaded == 0:
        # Not a failure: an empty project / empty buckets is a valid state.
        print("Note: 0 files were found to back up (no buckets, or all empty).")
    # Optional "backup OK" email, same NOTIFY_ON_SUCCESS flag as the DB backup.
    if get_bool("NOTIFY_ON_SUCCESS", False):
        notify.send_success_alert(file_name, size_bytes, timestamp)
    print("Done.")
    print(
        f"SUCCESS: {file_name} ({human_size(size_bytes)}) — "
        f"{total_downloaded} file(s) archived, {total_failed} skipped, "
        f"in {duration:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
