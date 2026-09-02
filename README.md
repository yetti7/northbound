# Northbound

Northbound is a self-hosted reading challenge application for reading Groups and
their Readers. It replaces scattered submission forms and score spreadsheets with
shared Challenges, review workflows and durable reading records.

**V1 reading history is Challenge-generated.** Northbound is not a general personal
reading tracker and does not import personal reading history.

## What V1 supports

- Accounts and multiple Groups with Group Owner, Moderator and Member authority.
- Challenge registration and lifecycle, teams, Hosts, Team Leaders and Floaters.
- Completed-book submissions and review, Themes, Book of the Month and Personal TBR.
- Manual Rewards / Games, checkpoints, and team/participant reporting, keeping
  original submissions, approved base pages and reward adjustments distinct.
- Platform Administration, audit history, controlled recovery, and SQLite backups/restores.
- Optional Hardcover catalog lookup and Reader-owned, explicitly consented library sync.

## Normal deployment

Run **one Northbound application container**, SQLite, and a persistent `/data`
volume. SQLite is the normal choice for a small self-hosted installation. The
container includes its schedulers and Hardcover worker; no Redis, Celery or internal
proxy container is required. Use an external reverse proxy for public HTTPS.

[PostgreSQL is optional](docs/SELF_HOSTING.md#optional-postgresql), not a prerequisite.
Keep one application container even with PostgreSQL.

## Quick start

You need Docker Engine with Docker Compose v2, Git, OpenSSL and persistent local
storage. Obtain the configuration/source files for the version you intend to run:

```sh
git clone https://github.com/yetti7/northbound.git northbound
cd northbound
cp .env.example .env
chmod 600 .env
```

Before starting, edit `.env`:

1. Replace `DJANGO_SECRET_KEY` with a strong random secret; keep `DJANGO_DEBUG=0`.
2. Set `NORTHBOUND_URL` to the address Readers will use, including scheme and any
   nonstandard port. The example uses `http://localhost:8000`.
3. Set `NORTHBOUND_BIND_ADDRESS=127.0.0.1` for same-host private setup. Complete
   first-run setup before public exposure; use an SSH tunnel or restricted network
   for initial remote access.
4. Strongly recommended: configure `NORTHBOUND_TOKEN_ENCRYPTION_KEY` **before saving
   integration credentials**. See [configuration and key generation](docs/SELF_HOSTING.md#configuration).

```sh
docker compose pull
docker compose up -d
docker compose ps
curl --fail --silent --show-error http://127.0.0.1:8000/health/
```

Compose creates persistent storage on first start and selects
`ghcr.io/yetti7/northbound:latest`; it does not build the checkout. Registry access
and your intended image/version must be available—an unpublished checkout is not
necessarily in that image. The healthy response is plain `OK`. Adjust the probe
address if you changed the bind address or host port.

Open `/setup/` at your configured address and create the first Platform Owner.
This is an account-creation form, not a default password or environment-based owner
bootstrap. Review **Platform Administration → Settings**, configure backups and
then optionally Hardcover. See [Self-hosting](docs/SELF_HOSTING.md) for detailed
setup, HTTPS, PostgreSQL and source builds.

## Protect your data before upgrading

Preserve `/data/northbound.sqlite3`, `/data/media/` and your installation secrets.
Stored SQLite backups normally live at `/data/backups/`; download copies to separate
protected storage. A backup on the same volume does not protect against volume loss.
Never commit `.env`, keys, credentials, databases, backups or uploaded media.

Keep the **original encryption key**: encrypted credentials in a restored database
need the same key. Back up before upgrading. Startup runs migrations, so switching
to an older image alone is not a safe rollback strategy. Read
[Backup and restore](docs/BACKUP_RESTORE.md) and [Upgrades](docs/SELF_HOSTING.md#upgrades)
first. Do not remove the persistent volume.

## Hardcover, optionally

A Group's scoped API key powers shared catalog/search/metadata workflows.
A Reader's separate OAuth connection—or personal scoped API key—powers their
Personal TBR lookup and optional personal synchronization. Neither falls back to
the other. Each installation brings its own OAuth Developer App.

**Connection is not consent.** Sync defaults OFF. Only new eligible approvals after
the Reader enables consent may synchronize; date sync also requires completed-book
consent. Previously approved history stays Northbound-only: there is no backfill
or Hardcover-history import. Sync Now handles existing queue work only. Provider
availability does not determine Northbound approval or scoring.
See [Hardcover setup and operations](docs/HARDCOVER.md).

## Documentation

| Topic | Canonical guide |
| --- | --- |
| Manage your Group and oversee Challenges | [Group Owner Guide](docs/GROUP_OWNER_GUIDE.md) |
| Configure and run a Challenge | [Host Guide](docs/HOST_GUIDE.md) |
| Register, submit books and follow your reading | [Reader Guide](docs/READER_GUIDE.md) |
| Install, configuration, first run, upgrades, development | [Self-hosting](docs/SELF_HOSTING.md) |
| Backups, restore, rollback and restored sync safety | [Backup and restore](docs/BACKUP_RESTORE.md) |
| Public origins, proxy headers and HTTPS | [Reverse proxies](docs/REVERSE_PROXY.md) |
| Credentials, OAuth and sync | [Hardcover](docs/HARDCOVER.md) |
| Architecture and data boundaries | [Project overview](docs/PROJECT_OVERVIEW.md) |
| Brief authority/lifecycle reference | [Roles and lifecycle](docs/ROLES_AND_LIFECYCLE.md) |

The public V1 source includes runtime code, production migrations, source-build
configuration and these guides. The full development test suite, demo seeder and
internal validation tools remain in the development workspace, not this public
package. Public CI checks source configuration, migrations and container builds;
it does not claim to run the private development regression suite.
