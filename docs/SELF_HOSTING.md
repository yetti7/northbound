# Self-hosting Northbound

Canonical installation, configuration and upgrade guidance. Obtain repository files
using the [README quick start](../README.md#quick-start).
[Backup and restore](BACKUP_RESTORE.md), [Reverse proxies](REVERSE_PROXY.md) and
[Hardcover](HARDCOVER.md) cover their respective operational procedures.

## Supported layout

`compose.yaml` selects `ghcr.io/yetti7/northbound:latest`, publishes host port 8000
by default, and mounts the named volume `northbound_data` at `/data`. Compose
creates it on first start. With the default project name its Docker name is
`northbound_northbound_data`; a project-name override changes that name.

| Persistent content | Default container path |
| --- | --- |
| SQLite database | `/data/northbound.sqlite3` |
| Uploaded media | `/data/media/` |
| Stored SQLite backup ZIPs | `/data/backups/` |
| Restore staging, markers and pre-restore copies | Under `/data/` |

Keep the project name and volume configuration consistent between runs.
`docker compose down` retains named volumes; adding `--volumes` deletes them.
Do not use that option for upgrades or routine stops.

Use SQLite on persistent local storage for the normal small self-hosted deployment,
not a remote network filesystem. The image runs as UID/GID 10001; a substitute bind
mount needs write access for that account. PostgreSQL does not remove media
persistence requirements or permit multiple application replicas. Keep one
Northbound application container: the sync lock is local, not distributed.

## Configuration

Compose loads `.env` through its supplied `env_file` configuration.
Keep a protected copy separately from database backups. The example is not a
working production secret configuration. Never publish rendered Compose configuration
or environment dumps; these can include secrets.

Generate new secrets locally, not in public terminal recordings, logs or issues:

```sh
# Copy this output into DJANGO_SECRET_KEY in .env.
openssl rand -hex 32
# Copy this output into NORTHBOUND_TOKEN_ENCRYPTION_KEY in .env.
# Fernet requires URL-safe base64 encoding of 32 random bytes.
openssl rand -base64 32 | tr '/+' '_-'
```

These commands are for **new installations**. Preserve existing keys; generating
replacements can make saved credentials unreadable.

| Setting | Requirement and effect |
| --- | --- |
| `DJANGO_SECRET_KEY` | Required strong persistent secret. Production rejects the development fallback and `replace-` placeholders. |
| `DJANGO_DEBUG=0` | Required for production; supplied in the example. Bare Python otherwise defaults to debug mode. |
| `NORTHBOUND_URL` | Required for this setup: full origin such as `https://northbound.example.org`, without path/query/fragment or credentials. Adds its hostname to allowed hosts and origin to CSRF trusted origins; determines OAuth callback URLs. |
| `NORTHBOUND_TOKEN_ENCRYPTION_KEY` | Strongly recommended before saving credentials: persistent dedicated Fernet key. A malformed configured key prevents startup. |
| `NORTHBOUND_BIND_ADDRESS` | Recommended explicit binding: `127.0.0.1` for same-host setup/proxy. Compose otherwise defaults to `0.0.0.0`; remote proxies/LAN clients need a reachable, firewall-restricted address. |
| `NORTHBOUND_PORT` | Optional host port, default `8000`. Include nonstandard ports in the public URL for direct access. |
| `PORT` | Optional internal port, default `8000`; startup, Compose and healthcheck use it consistently. Normally leave unchanged. |
| `NORTHBOUND_SERVE_MEDIA` | Compose defaults to `1`, serving media through the application without a separate media container. |
| `NORTHBOUND_TRUST_PROXY_HEADERS` | Default OFF; enable only behind a trusted, restricted proxy that overwrites forwarded headers. |
| `TIME_ZONE` | Initial/fallback timezone, default `America/New_York`. Review the persisted Platform timezone in General Settings; backup schedules use that setting. |
| `WEB_CONCURRENCY` | Optional Gunicorn process count, default `2`; not extra application containers or sync workers. |

Advanced settings include comma-separated `DJANGO_ALLOWED_HOSTS` and
`DJANGO_CSRF_TRUSTED_ORIGINS`; see the [proxy guide](REVERSE_PROXY.md) for HTTPS
options. The example documents profile/request size limits.
`NORTHBOUND_MAX_BACKUP_BYTES` limits authenticated backup uploads (default 1 GiB);
a proxy may impose a smaller limit.

Compose explicitly fixes `NORTHBOUND_SQLITE_PATH=/data/northbound.sqlite3` and
`NORTHBOUND_MEDIA_ROOT=/data/media`. Different values in `.env` do not override
those service settings. Outside Compose these variables are supported, with
checkout-local defaults `db.sqlite3` and `media/`.
`NORTHBOUND_SQLITE_TIMEOUT` defaults to 20 seconds. Plan and back up any storage move.

### Preserve the encryption key

Protected Group API keys, Reader PATs/OAuth tokens and the installation OAuth Client
Secret use `NORTHBOUND_TOKEN_ENCRYPTION_KEY`. If empty, a key is derived from the
exact `DJANGO_SECRET_KEY` instead.

**Adding a dedicated key to an existing fallback-key installation is not a
transparent upgrade.** There is no automatic re-encryption/key migration. Preserve
the original dedicated key, or original Django secret for fallback use. Lost/changed
keys require restoring the original key or deliberately replacing/reconnecting
affected integrations. Protect keys separately from backups. See
[restore implications](BACKUP_RESTORE.md#keys-and-restored-hardcover-work).

## First run

1. Keep access private until the owner is created. Start using the README commands;
   Compose provisions the volume automatically.
2. Open `/setup/`. Fresh empty installations redirect normal page requests there.
   Enter a username, unique email and password to create the first Platform Owner;
   there is no preset owner password.
3. Once an owner exists, first-owner setup is unavailable. Sign in at
   `/config/login/`; Platform Administration is `/config/`.
4. In **Settings → General Settings**, review the name, timezone, public registration
   and ordinary-account Group creation. Platform Owner access does not create
   Group membership or competing Reader identity.
5. In **Settings → Backups**, configure schedule/retention, create a stored backup,
   and download an off-volume copy. PostgreSQL requires native backups.
6. Configure [HTTPS/proxy access](REVERSE_PROXY.md) before public use, then optionally
   **Settings → Hardcover OAuth**. Group and Reader connections remain separate.

Normal deployments start without demo data. The demo seeder and dataset are
development-workspace tools, intentionally omitted from public V1 source and images.

## Startup and health

`deploy/start.sh` applies any pending SQLite restore, runs migrations, then applies
restored-Hardcover safeguards. Only afterward does it start the backup scheduler,
Challenge scheduler, Hardcover worker and Gunicorn. PostgreSQL skips SQLite backup
scheduling. There is no separate Redis/Celery requirement.

Challenge automation and Hardcover processing normally poll every 30 seconds;
automatic backups follow their configured schedule. Do not duplicate these workers.

`GET /health/` is unauthenticated: database `SELECT 1` produces plain `OK`/200
or `UNAVAILABLE`/503 on a database error. This is not a migration, scheduler or
Hardcover check. Inspect backup and Reader sync status separately.

```sh
docker compose ps
docker compose exec web python manage.py migrate --check
```

Inspect startup logs privately on failure. Do not publish credentials, callback
queries, raw debug tracebacks or configuration dumps.

## Optional PostgreSQL

Choose PostgreSQL before initializing a new installation. Adding this override to
existing SQLite **does not migrate its data**.

Set a strong `POSTGRES_PASSWORD` in `.env`; optionally set `POSTGRES_DB` and
`POSTGRES_USER` (both default `northbound`). Use both files on lifecycle commands:

```sh
docker compose -f compose.yaml -f compose.postgres.yaml pull
docker compose -f compose.yaml -f compose.postgres.yaml up -d
docker compose -f compose.yaml -f compose.postgres.yaml ps
```

The override adds PostgreSQL 17, its `postgres_data` volume and database health
dependency. It sets `POSTGRES_HOST=db`, port `5432`, and matching credentials
for Northbound. Preserve `/data` for media as well as PostgreSQL data.
Built-in SQLite ZIP backup/restore is unavailable on PostgreSQL.

External PostgreSQL can use `DATABASE_URL` or `POSTGRES_HOST`,
`POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`.
Precedence is `DATABASE_URL`, then `POSTGRES_HOST`, then SQLite. Remove stale
`DATABASE_URL` configuration when using the bundled override. External connection
security and native backup/restore are operator responsibilities. Only SQLite and
PostgreSQL are documented deployment choices.

## Upgrades

Plan a maintenance window; there is no zero-downtime or automatic rollback guarantee.

1. Read target release notes and obtain matching Compose/example files. Review
   changes without overwriting `.env` or changing the volume/project.
2. Create/download a pre-upgrade backup. Preserve keys, configuration and the exact
   previous image reference separately. PostgreSQL needs native database backups
   and matching media. See [Backup and restore](BACKUP_RESTORE.md).
3. For SQLite, pull and recreate:

   ```sh
   docker compose pull
   docker compose up -d
   docker compose ps
   ```

   For PostgreSQL include `-f compose.yaml -f compose.postgres.yaml` on each command.
   The default `latest` tag moves; select an available version tag or digest in
   deployment configuration for repeatable image selection.
4. Startup migrates automatically. Verify health, owner login, expected Groups/
   Challenges, media, backups and Reader sync status. Retain the pre-upgrade backup.

After editing `.env`, use `docker compose up -d` to recreate the service;
`docker compose restart` does not apply environment changes.
Do not run an older image against a newer schema unless compatibility is established.
See [rollback and recovery](BACKUP_RESTORE.md#rollback-and-failed-upgrades).

## Source builds and local development

Build the checkout instead of pulling the default image with:

```sh
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

This selects `northbound:dev`. Review the checkout; Docker exclusions do not replace
keeping secrets out of source.

For isolated Python development, use Python 3.13 (matching the image):

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Bare Python does **not** load `.env` automatically; export needed settings in that
development shell. Never point this workflow at production data. In separate
activated terminals run `python manage.py run_challenge_scheduler` and, when testing
consented sync, `python manage.py run_hardcover_sync_worker`. Optional SQLite backup
scheduling uses `python manage.py run_backup_scheduler`. These processes are already
included in the normal container; do not duplicate them there.
