# Supabase Postgres Backup Tool + Dashboard

A small Python project that backs up a Supabase (Postgres) database, uploads the
backup to Azure Blob Storage, prunes old backups, records every run, and reports
failures by email. It also ships a clean web dashboard to view history and
trigger backups on demand.

- **`backup.py`** — the nightly database backup job (run from cron).
- **`storage_backup.py`** — a separate job that backs up Supabase Storage
  buckets (uploaded files). Run manually; see "Storage bucket backups" below.
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
| `backup.py`              | The Postgres database backup job.                              |
| `storage_backup.py`      | The Supabase Storage (uploaded files) backup job.              |
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
| `AZURE_CONTAINER_NAME` | yes | Container name (lowercase). Created if missing. Storage backups use the same container. |
| `SUPABASE_URL` | for storage backup | Project URL, e.g. `https://xxxx.supabase.co`. Settings → API → Project URL. |
| `SUPABASE_SERVICE_ROLE_KEY` | for storage backup | **service_role** key (not anon). Settings → API → Project API keys. Keep secret. |
| `BACKUP_BUCKETS` | no | Comma-separated bucket names to back up. Blank = all buckets. |
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
links (short-lived Azure SAS URLs), and a "Run backup now" button. The runs
table has a **Type** column so you can tell Database backups from Storage
backups at a glance.

---

## Storage bucket backups (uploaded files)

`storage_backup.py` is a **separate** job from `backup.py`. `backup.py` dumps the
Postgres *database*; `storage_backup.py` backs up the *files* your users have
uploaded to Supabase **Storage buckets**. It is **strictly read-only** on
Supabase — it only lists and downloads, and never deletes, overwrites, or
changes anything in your buckets.

What a run does:

1. Connects to Supabase with `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`.
2. Backs up every bucket, or just the ones named in `BACKUP_BUCKETS`.
3. Downloads all files (paging through large buckets, recursing into folders,
   and skipping + logging any single file that fails) into a temp directory,
   preserving `bucket/folder/file` structure.
4. Packs them into one archive: `storage-backup-YYYY-MM-DD-HHMM.tar.gz`.
5. Uploads that archive to the **same** Azure container as the DB backups, then
   prunes only old `storage-backup-*.tar.gz` blobs (never the DB backups).
6. Records the run in the shared history (`backups.db` / `backups.log`) tagged
   as **storage**, and cleans up the temp dir + local archive (respecting
   `BACKUP_KEEP_LOCAL`).

> **You need the service-role key.** Only the `service_role` key can read every
> bucket (including private ones). Get it from Supabase → Project Settings →
> API → Project API keys → `service_role`. Treat it like a password — it grants
> full access to your project. It lives only in `.env`.

### Run it manually

Preview first with `--dry-run` — it connects, lists buckets, and prints how many
files (and what size) it *would* back up, but downloads nothing, uploads
nothing, and writes no history. It doesn't even need the Azure config:

```bash
source venv/bin/activate
python storage_backup.py --dry-run
```

Then do a real run:

```bash
python storage_backup.py
```

Expected on success: a new `storage-backup-*.tar.gz` locally and in Azure, a
`SUCCESS` row in `backups.log` / `backups.db` (type `storage`), and exit code
`0`. A failed run emails an alert (same `notify.py` as the DB backup) and, if
`NOTIFY_ON_SUCCESS=true`, a successful run emails an "OK" note too. It is **not** wired into the nightly cron yet — run and verify it on its
own first. Once you're happy, you can add it to `crontab` just like `backup.py`
(e.g. a second nightly line calling `python storage_backup.py`).

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
