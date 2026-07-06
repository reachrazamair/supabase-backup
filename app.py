#!/usr/bin/env python3
"""
Flask dashboard for the Supabase backup tool.

Shows backup history from the SQLite database (db.py), lets an authenticated
user trigger a backup, and offers short-lived download links to the copies in
Azure Blob Storage. It is READ-ONLY with respect to your data — it never
connects to Postgres; the only action it can take is launching backup.py as a
separate process.

Login is session-based and gated on PANEL_USER / PANEL_PASSWORD from .env. Those
plus FLASK_SECRET_KEY are required; the app refuses to start (fails loud) if any
is unset.

Local dev:   python app.py           (http://127.0.0.1:8000)
Production:  run under gunicorn + systemd (see README.md).
"""

import functools
import hmac
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import db
from config import get_env, require_env

SCRIPT_DIR = Path(__file__).resolve().parent
BACKUP_SCRIPT = SCRIPT_DIR / "backup.py"

app = Flask(__name__)

# Required for signed session cookies and for auth. require_env fails loud if
# any of these are missing, so a misconfigured deploy stops here rather than
# silently running an insecure dashboard.
app.secret_key = require_env("FLASK_SECRET_KEY")
PANEL_USER = require_env("PANEL_USER")
PANEL_PASSWORD = require_env("PANEL_PASSWORD")

# How long a generated Azure download link stays valid.
DOWNLOAD_LINK_MINUTES = 15


# --------------------------------------------------------------------------- #
# Formatting helpers exposed to templates
# --------------------------------------------------------------------------- #

def human_size(num_bytes):
    """Format a byte count as a short human-readable string."""
    if not num_bytes:
        return "—"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024


def human_duration(seconds):
    """Format a duration in seconds as e.g. '4.2s' or '1m 05s'."""
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s"


def pretty_time(iso_timestamp):
    """Render an ISO timestamp as 'YYYY-MM-DD HH:MM UTC' for display."""
    if not iso_timestamp:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso_timestamp


def backup_type_label(backup_type):
    """Human label for a run's backup type: 'Storage' vs 'Database'."""
    if backup_type == db.TYPE_STORAGE:
        return "Storage"
    return "Database"


# Make the helpers available inside Jinja templates as filters.
app.jinja_env.filters["human_size"] = human_size
app.jinja_env.filters["human_duration"] = human_duration
app.jinja_env.filters["pretty_time"] = pretty_time
app.jinja_env.filters["backup_type_label"] = backup_type_label


# --------------------------------------------------------------------------- #
# Azure download links (generated locally — no network call)
# --------------------------------------------------------------------------- #

def build_download_url(file_name):
    """
    Build a short-lived, read-only SAS URL for a backup blob.

    The SAS token is signed locally using the account key from the connection
    string, so this makes no network request and needs no live Azure session.
    Returns None if Azure is not configured or the connection string cannot be
    parsed — the template then shows a "stored remotely" badge instead.
    """
    conn = get_env("AZURE_STORAGE_CONNECTION_STRING")
    container = get_env("AZURE_CONTAINER_NAME")
    if not conn or not container or not file_name:
        return None

    # Connection strings are "Key=Value;Key=Value;..."; values (like the base64
    # account key) can themselves contain '=', so split on the FIRST '=' only.
    parts = dict(
        piece.split("=", 1) for piece in conn.split(";") if "=" in piece
    )
    account = parts.get("AccountName")
    account_key = parts.get("AccountKey")
    suffix = parts.get("EndpointSuffix", "core.windows.net")
    if not account or not account_key:
        return None

    # Imported lazily so the dashboard still loads if azure isn't installed yet.
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas

    sas = generate_blob_sas(
        account_name=account,
        container_name=container,
        blob_name=file_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(minutes=DOWNLOAD_LINK_MINUTES),
    )
    return f"https://{account}.blob.{suffix}/{container}/{file_name}?{sas}"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def login_required(view):
    """Decorator that redirects to the login page if not authenticated."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def _credentials_valid(username, password):
    """Constant-time comparison of submitted credentials against .env."""
    user_ok = hmac.compare_digest(username, PANEL_USER)
    pass_ok = hmac.compare_digest(password, PANEL_PASSWORD)
    return user_ok and pass_ok


@app.route("/login", methods=["GET", "POST"])
def login():
    """Render and handle the login form."""
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if _credentials_valid(username, password):
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        flash("Incorrect username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    """Clear the session and return to the login page."""
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

@app.route("/")
@login_required
def dashboard():
    """Show summary cards and the table of recent runs."""
    summary = db.get_summary()
    runs = db.list_runs(limit=100)

    # Attach a download URL to each successful run that has a file name.
    rows = []
    for run in runs:
        download_url = None
        if run.status == db.STATUS_SUCCESS and run.file_name:
            download_url = build_download_url(run.file_name)
        rows.append({"run": run, "download_url": download_url})

    return render_template("dashboard.html", summary=summary, rows=rows)


@app.route("/run-backup", methods=["POST"])
@login_required
def run_backup():
    """
    Launch backup.py as a separate background process.

    We deliberately do NOT block the web request waiting for the backup (it can
    take minutes). The process records its own result to the database, so the
    table shows the outcome once the user refreshes.
    """
    try:
        subprocess.Popen(
            [sys.executable, str(BACKUP_SCRIPT)],
            cwd=str(SCRIPT_DIR),
        )
        flash("Backup started. Refresh in a moment to see the result.", "success")
    except Exception as exc:  # noqa: BLE001 - report, never crash the page
        flash(f"Could not start backup: {exc}", "error")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    # Local development server only. Use gunicorn in production (see README).
    app.run(host="127.0.0.1", port=8000, debug=False)
