"""
Email notifications over SMTP.

Failures always send an alert; successes send one only when NOTIFY_ON_SUCCESS
is enabled (the caller checks that flag). Every function here is defensive:
a broken SMTP server, bad credentials, or missing config must NEVER crash the
backup itself — the backup running is more important than the email arriving.
So all failures are caught and reported to stderr, and the functions return a
bool indicating whether the mail was actually sent.
"""

import smtplib
import sys
import urllib.request
from email.message import EmailMessage

from config import get_env

# Required-for-email variables. If any are missing we skip sending (and say so)
# rather than raising — email is a "nice to have" layered on top of the backup.
_SMTP_KEYS = ("SMTP_HOST", "SMTP_PORT", "ALERT_FROM", "ALERT_TO")


def _smtp_settings():
    """
    Collect SMTP settings from the environment.

    Returns a dict of settings, or None if any required value is missing.
    SMTP_USER / SMTP_PASSWORD are optional (some relays allow unauthenticated
    send from trusted hosts).
    """
    missing = [key for key in _SMTP_KEYS if not get_env(key)]
    if missing:
        print(
            f"WARNING: email not sent — missing SMTP config: {', '.join(missing)}",
            file=sys.stderr,
        )
        return None

    # Parse the port defensively: a non-numeric SMTP_PORT must degrade to a
    # skipped email, never raise and crash the backup run. See H7 fix.
    port_raw = get_env("SMTP_PORT")
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        print(
            f"WARNING: email not sent — SMTP_PORT={port_raw!r} is not a valid number",
            file=sys.stderr,
        )
        return None

    return {
        "host": get_env("SMTP_HOST"),
        "port": port,
        "user": get_env("SMTP_USER"),
        "password": get_env("SMTP_PASSWORD"),
        "sender": get_env("ALERT_FROM"),
        # ALERT_TO may be a comma-separated list of recipients.
        "recipients": [r.strip() for r in get_env("ALERT_TO").split(",") if r.strip()],
    }


def _send_email(subject: str, body: str) -> bool:
    """
    Send a plain-text email. Returns True on success, False on any problem.

    Chooses implicit TLS (SMTP_SSL) for port 465 and STARTTLS otherwise, which
    covers the common provider configurations.
    """
    settings = _smtp_settings()
    if settings is None:
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings["sender"]
    message["To"] = ", ".join(settings["recipients"])
    message.set_content(body)

    try:
        if settings["port"] == 465:
            with smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=30) as smtp:
                _login_and_send(smtp, settings, message)
        else:
            with smtplib.SMTP(settings["host"], settings["port"], timeout=30) as smtp:
                smtp.starttls()
                _login_and_send(smtp, settings, message)
        return True
    except Exception as exc:  # noqa: BLE001 - email must never break the backup
        print(f"WARNING: failed to send alert email: {exc}", file=sys.stderr)
        return False


def _login_and_send(smtp, settings, message) -> None:
    """Authenticate (if credentials are provided) and send the message."""
    if settings["user"] and settings["password"]:
        smtp.login(settings["user"], settings["password"])
    smtp.send_message(message)


def send_failure_alert(error_message: str, timestamp: str, log_tail: str) -> bool:
    """Notify that a backup run failed, including recent log lines."""
    subject = "[Supabase backup] FAILED"
    body = (
        "A Supabase backup run FAILED.\n\n"
        f"Time (UTC): {timestamp}\n"
        f"Error: {error_message}\n\n"
        "Last log lines:\n"
        "----------------------------------------\n"
        f"{log_tail or '(log is empty)'}\n"
    )
    return _send_email(subject, body)


def ping_heartbeat(url: str) -> bool:
    """
    Best-effort HTTP GET to a heartbeat / dead-man's-switch URL.

    Called only after a fully successful backup so an external monitor (e.g.
    healthchecks.io) can alert if the expected ping stops arriving. Like the
    email helpers, this never raises — a failed ping must not affect the backup
    result. Returns True if the ping succeeded. See H6 fix.
    """
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            response.read()  # drain the response so the connection closes cleanly
        return True
    except Exception as exc:  # noqa: BLE001 - heartbeat must never break the backup
        print(f"WARNING: heartbeat ping failed: {exc}", file=sys.stderr)
        return False


def send_success_alert(file_name: str, size_bytes: int, timestamp: str) -> bool:
    """Notify that a backup run succeeded (only called when the flag is on)."""
    subject = "[Supabase backup] OK"
    body = (
        "Supabase backup completed successfully.\n\n"
        f"Time (UTC): {timestamp}\n"
        f"File: {file_name}\n"
        f"Size: {size_bytes} bytes\n"
    )
    return _send_email(subject, body)
