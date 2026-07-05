"""
SQLite data-access layer for backup run history.

Every backup run (success or failure) is recorded in `backups.db` so the
dashboard can show history without parsing the plain-text log. This module is
the ONLY place that talks to SQLite — callers use the typed helpers below and
never write SQL themselves.

Schema (table `runs`):
    id                INTEGER  primary key
    timestamp         TEXT     UTC ISO-8601, when the run started
    status            TEXT     'success' or 'fail'
    file_name         TEXT     backup file name (nullable)
    file_size_bytes   INTEGER  size of the gzip (nullable)
    duration_seconds  REAL     wall-clock seconds the run took (nullable)
    error_message     TEXT     failure reason, NULL on success
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "backups.db"

# Allowed status values, kept in one place so callers stay consistent.
STATUS_SUCCESS = "success"
STATUS_FAIL = "fail"


@dataclass
class BackupRun:
    """One row in the `runs` table. `id` is set by the database on insert."""

    timestamp: str
    status: str
    file_name: Optional[str] = None
    file_size_bytes: Optional[int] = None
    duration_seconds: Optional[float] = None
    error_message: Optional[str] = None
    id: Optional[int] = None


@dataclass
class Summary:
    """Aggregate stats for the dashboard summary cards."""

    total_runs: int
    last_status: Optional[str]
    last_timestamp: Optional[str]
    total_size_bytes: int


@contextmanager
def _connect():
    """
    Yield a SQLite connection, commit on success, and ALWAYS close it.

    Using this as a context manager (`with _connect() as conn:`) guarantees the
    connection is closed even on error, so connections can't leak across the
    dashboard's frequent reads. `busy_timeout` makes a reader wait briefly for a
    concurrent writer instead of immediately raising "database is locked". M6.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")  # milliseconds
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create the `runs` table if it does not already exist (idempotent)."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp        TEXT    NOT NULL,
                status           TEXT    NOT NULL,
                file_name        TEXT,
                file_size_bytes  INTEGER,
                duration_seconds REAL,
                error_message    TEXT
            )
            """
        )


def insert_run(run: BackupRun) -> int:
    """Insert a run record and return its new database id."""
    init_db()  # cheap and idempotent; guarantees the table exists.
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO runs
                (timestamp, status, file_name, file_size_bytes,
                 duration_seconds, error_message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run.timestamp,
                run.status,
                run.file_name,
                run.file_size_bytes,
                run.duration_seconds,
                run.error_message,
            ),
        )
        return int(cursor.lastrowid)


def list_runs(limit: int = 100) -> List[BackupRun]:
    """Return the most recent runs, newest first (up to `limit`)."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        BackupRun(
            id=row["id"],
            timestamp=row["timestamp"],
            status=row["status"],
            file_name=row["file_name"],
            file_size_bytes=row["file_size_bytes"],
            duration_seconds=row["duration_seconds"],
            error_message=row["error_message"],
        )
        for row in rows
    ]


def get_summary() -> Summary:
    """Return aggregate stats used by the dashboard summary cards."""
    init_db()
    with _connect() as conn:
        total_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        # Only successful backups contribute to stored size.
        total_size = conn.execute(
            "SELECT COALESCE(SUM(file_size_bytes), 0) FROM runs WHERE status = ?",
            (STATUS_SUCCESS,),
        ).fetchone()[0]
        last = conn.execute(
            "SELECT status, timestamp FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    return Summary(
        total_runs=total_runs,
        last_status=last["status"] if last else None,
        last_timestamp=last["timestamp"] if last else None,
        total_size_bytes=int(total_size),
    )
