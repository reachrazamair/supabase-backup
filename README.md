# Supabase Postgres Backup Tool + Dashboard

A small Python project that backs up a Supabase (Postgres) database, uploads the
backup to Azure Blob Storage, prunes old backups, records every run, and reports
failures by email. It also ships a clean web dashboard to view history and
trigger backups on demand.

- **`backup.py`** — the nightly backup job (run from cron).
- **`app.py`** — the Flask dashboard (kept running by systemd).

## What a backup run does

1. Runs `pg_dump` on the full database (from `DATABASE_URL`) and gzips it to a
   timestamped file, e.g. `backup-2026-06-30-0200.sql.gz`.
2. Uploads that file to an Azure Blob Storage container.
3. Deletes blobs older than `RETENTION_DAYS` (default 90) from the container.
4. Counts the run as a **failure** if `pg_dump` failed, the upload failed, or
   the gzip file is empty / smaller than `MIN_BACKUP_BYTES`.
5. On failure, emails the error + timestamp + recent log lines. On success it
   can optionally email an "OK" note (`NOTIFY_ON_SUCCESS`). A broken mail server
   never stops a backup.
6. Records the run to **`backups.db`** (SQLite) *and* appends a line to
   **`backups.log`** (timestamp, status, file, size).

All secrets live in `.env` only. Nothing is hardcoded.

---

## Project layout

| File / folder            | Purpose                                                        |
|--------------------------|----------------------------------------------------------------|
| `backup.py`              | The backup job.                                                |
| `app.py`                 | The Flask dashboard.                                           |
| `config.py`              | Loads `.env`; `require_env()` fails loud on missing vars.      |
| `db.py`                  | SQLite history: `init_db` / `insert_run` / `list_runs`.        |
| `notify.py`              | SMTP email alerts (failure always, success optional).          |
| `templates/`             | Jinja HTML (`base`, `login`, `dashboard`).                     |
| `static/style.css`       | Hand-written dashboard styling.                                |
| `supabase-dashboard.service` | systemd unit to keep the dashboard running.               |
| `.env` / `.env.example`  | Secrets/config (real / template).                              |
| `backups.log` / `backups.db` | Run history (git-ignored, created on first run).           |

---

## 1. Requirements

- **Python 3.8+**
- **PostgreSQL client tools** — the backup calls `pg_dump`, which must be on the
  `PATH`. Match the major version to your database where possible.

  ```bash
  # Debian / Ubuntu
  sudo apt-get update && sudo apt-get install -y postgresql-client
  ```

> Tip: `pg_dump --version` should be ≥ your Supabase Postgres major version.
> An older client can refuse to dump a newer server.

## 2. Install

```bash
cd /path/to/supabase-backup

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Configure `.env`

```bash
cp .env.example .env
nano .env
```

Fill in these variables (grouped as in `.env.example`):

| Variable | Required | What to put |
|---|---|---|
| `DATABASE_URL` | yes | Supabase URI: Project Settings → Database → Connection string → URI. |
| `AZURE_STORAGE_CONNECTION_STRING` | yes | Azure → Storage account → Access keys → Connection string. |
| `AZURE_CONTAINER_NAME` | yes | Container name (lowercase). Created if missing. |
| `SMTP_HOST` / `SMTP_PORT` | for email | Mail server host and port (587 STARTTLS or 465 SSL). |
| `SMTP_USER` / `SMTP_PASSWORD` | for email | SMTP credentials (optional if your relay allows unauthenticated send). |
| `ALERT_FROM` / `ALERT_TO` | for email | From address and recipient(s); `ALERT_TO` may be comma-separated. |
| `NOTIFY_ON_SUCCESS` | no | `true` to email on success too. Default `false`. |
| `PANEL_USER` / `PANEL_PASSWORD` | for dashboard | Dashboard login. App refuses to start if unset. |
| `FLASK_SECRET_KEY` | for dashboard | Session-signing key. Generate: `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `MIN_BACKUP_BYTES` | no | Smaller finished backups are treated as failures. Default `1024`. |
| `RETENTION_DAYS` | no | Delete backups older than this many days. Default `90` (values under 7 are clamped up to 7). |
| `BACKUP_KEEP_LOCAL` | no | How many recent backups to keep on local disk after upload. Default `1`; `0` = keep none. |
| `HEARTBEAT_URL` | no | If set, pinged after a fully successful backup (dead-man's switch). See below. |

The `.env` file is git-ignored — keep it that way.

### Heartbeat / dead-man's switch (recommended)

Failure emails only fire when a backup *runs* and fails. If cron is broken or
the VM is down, the job never runs and you'd hear nothing. To catch that, set
`HEARTBEAT_URL` to a monitor like [healthchecks.io](https://healthchecks.io):
`backup.py` sends a plain HTTP GET to that URL only after a fully successful
backup + upload, and the monitor alerts you if the expected ping stops arriving.
Leave it blank to disable.

## 4. Test-run the backup once

```bash
source venv/bin/activate
python backup.py
```

Expected on success: a new `backup-*.sql.gz` locally and in Azure, a `SUCCESS`
row in `backups.log` and `backups.db`, and exit code `0`. To exercise the
failure path, point `DATABASE_URL` at a bad host — you should get a `FAILURE`
record, a failure email (if SMTP is set), and exit code `1`.

## 5. Run the dashboard locally

```bash
source venv/bin/activate
python app.py
# open http://127.0.0.1:8000  and log in with PANEL_USER / PANEL_PASSWORD
```

The dashboard shows summary cards, a table of recent runs, per-row download
links (short-lived Azure SAS URLs), and a "Run backup now" button.

---

## 6. Deploy to an Ubuntu VM

Assuming the project lives at `/opt/supabase-backup` and runs as user `ubuntu`.

```bash
# 1. System packages
sudo apt-get update
sudo apt-get install -y python3-venv postgresql-client

# 2. Get the code onto the box, then set up the venv
sudo mkdir -p /opt/supabase-backup
sudo chown ubuntu:ubuntu /opt/supabase-backup
# ... copy the project files into /opt/supabase-backup ...
cd /opt/supabase-backup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Create and fill in .env
cp .env.example .env
nano .env
```

### 6a. Schedule the nightly backup (cron)

```bash
crontab -e
```

Add this line — it runs every night at **02:00 server time** and appends cron's
own output to a file so nothing is lost:

```cron
0 2 * * * cd /opt/supabase-backup && /opt/supabase-backup/venv/bin/python backup.py >> /opt/supabase-backup/cron.out 2>&1
```

Notes:
- Using the venv's Python directly means cron does not need the venv "activated".
- Cron uses the **server's local timezone**. Check with `timedatectl` and adjust
  the hour if you want 2 AM in a specific zone.

### 6b. Keep the dashboard running (systemd + gunicorn)

`gunicorn` is already in `requirements.txt`. Edit `supabase-dashboard.service`
if your `User`, `WorkingDirectory`, or venv path differ, then:

```bash
sudo cp supabase-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now supabase-dashboard
systemctl status supabase-dashboard        # confirm it's active
```

The service binds gunicorn to `127.0.0.1:8000`. For public/HTTPS access, put a
reverse proxy (e.g. nginx with a TLS cert) in front of it — do **not** expose
port 8000 directly to the internet.

## Housekeeping

The backup does **not** delete local `*.sql.gz` files — only the Azure copies
are pruned. On a small VM, add a second cron line to clean local copies:

```cron
30 2 * * * find /opt/supabase-backup -name 'backup-*.sql.gz' -mtime +7 -delete
```
