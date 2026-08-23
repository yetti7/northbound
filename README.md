# Northbound

A self-hosted, browser-based reading challenge manager for long-running groups, monthly teams, verified page counts, configurable challenges, and visual progress trackers.

## Current State

Northbound currently includes:

- first-run platform setup;
- multiple reading groups and role-based memberships;
- challenge months and historical team assignments;
- book completion submissions;
- moderator approval with submitted, approved base, bonus, and final scored pages kept separately;
- monthly themes and reviewed bonus claims;
- group roles, per-member capability overrides, and immutable audit events;
- private reader statistics, announcements, and Hardcover-assisted catalog lookup.

See [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md) for the product direction.

## Self-host with Docker

Northbound's default deployment is one application container with SQLite and uploaded media in one persistent volume. It does not require PostgreSQL, Nginx, or another reverse proxy for local or LAN use.

```bash
cp .env.example .env
```

Edit `.env` and replace `DJANGO_SECRET_KEY` with a random value. For example, `openssl rand -hex 32` can generate one. Then pull and start Northbound:

```bash
docker compose pull
docker compose up -d
```

Compose pulls the published `ghcr.io/yetti7/northbound:latest` image, so a normal installation does not build Northbound from source. Open <http://localhost:8000/setup/> to create the first administrator account. SQLite and uploaded media persist across container recreation.

See [docs/SELF_HOSTING.md](docs/SELF_HOSTING.md) for LAN access, upgrades, backups, and external databases. See [docs/REVERSE_PROXY.md](docs/REVERSE_PROXY.md) before placing Northbound behind HTTPS, Cloudflare Tunnel, Nginx, Caddy, Traefik, or another proxy.

## Local Python development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Without `DATABASE_URL` or `POSTGRES_HOST`, Northbound uses SQLite. PostgreSQL remains supported for hosted and advanced deployments.

To build the current checkout in Docker instead of pulling the released image:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

## PostgreSQL

Use `compose.postgres.yaml` with the default file when PostgreSQL is preferred. See [docs/SELF_HOSTING.md](docs/SELF_HOSTING.md). The disposable DeepNorth test deployment will be reset separately after this image is published.
