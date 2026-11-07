# Development images

Pushes to `dev` publish only `ghcr.io/yetti7/northbound:dev` and
`ghcr.io/yetti7/northbound:dev-sha-<full-commit-sha>` after focused validation
and an image startup/data-volume smoke test. Both architectures are built from
the existing Dockerfile: linux/amd64 and linux/arm64. The development workflow
never publishes latest, stable version tags, generic SHA tags, or GitHub releases.
The stable container workflow and production Compose configuration are unchanged.

Both development tags identify the same image index. Record the index digest
and use a digest for repeatable deployments. The moving dev tag selects the
newest successful development build. NORTHBOUND_VERSION and OCI version metadata
are `dev-sha-<full-commit-sha>`; OCI revision is the full commit SHA.

## Runtime configuration

Start from `.env.dev.example`, generate fresh development-only application and
encryption secrets, and configure a separate /data mount. The existing SQLite
layout remains `/data/northbound.sqlite3`, `/data/media`, with backups beside the
database. Bind-mount directories must be writable by UID/GID 10001. Never mount
production data or reuse a production environment file. DATABASE_URL or POSTGRES_HOST
overrides the SQLite selection, so leave both unset. No new migrations are added.

Development-safe environment choices:

- `NORTHBOUND_BACKUP_SCHEDULER_ENABLED=0`: do not start automatic backup scheduling.
- `NORTHBOUND_CHALLENGE_SCHEDULER_ENABLED=0`: do not start Challenge scheduling.
- `NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED=0`: do not start the Hardcover worker;
  also disable sync capability, new queue entries, processing, and direct library
  mutations. Catalog read/search remains available with separate test credentials.
- `NORTHBOUND_DEVELOPMENT_BANNER=1`: show DEVELOPMENT on the shared page layout.
- `DJANGO_SESSION_COOKIE_NAME=northbound_dev_sessionid` and
  `DJANGO_CSRF_COOKIE_NAME=northbound_dev_csrftoken`: separate browser cookies.
- `DJANGO_DEBUG=0`, a fresh `DJANGO_SECRET_KEY`, and a development `NORTHBOUND_URL`.
- `NORTHBOUND_SQLITE_PATH=/data/northbound.sqlite3`,
  `NORTHBOUND_MEDIA_ROOT=/data/media`, `NORTHBOUND_SERVE_MEDIA=1`.

All three startup/background controls default to enabled, banner defaults to off,
and cookie names default to Django's sessionid/csrftoken. Existing production
behavior is preserved unless explicitly configured otherwise.

Startup still validates/applies any pending restore, migrates the configured
SQLite database, and quarantines restored sync events. Disabling schedulers does
not disable migrations or restore handling. Use a fresh development data directory;
never import a production backup without an approved, sanitized-copy procedure.

The DEVELOPMENT banner does not itself disable integrations or restrict network
access. Write-back controls do not disable read-only provider calls or OAuth account
configuration. Use fresh/test credentials and network restrictions separately.

No deployment to deepnorth is performed by this workflow. Host paths, Tailscale
access, development secrets, and any restricted deployment account require separate
setup and approval. The first development image includes the existing v1.0.8
baseline plus the pending Hardcover submission association/correction UX; remaining
v1.0.9 features are not included.
