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

## Quick start with Docker

Northbound's standard self-hosted deployment is one application container with SQLite and uploaded media together in one persistent `/data` volume. It does not require PostgreSQL, an internal Nginx container, or a separate database server.

```bash
cp .env.example .env
```

Edit `.env` and set:

- `DJANGO_SECRET_KEY` to a random value (for example, from `openssl rand -hex 32`);
- `NORTHBOUND_URL` to the address people will use; and
- `NORTHBOUND_PORT` if you do not want the default host port `8000`.

Then pull and start Northbound:

```bash
docker compose pull
docker compose up -d
```

Compose pulls the published `ghcr.io/yetti7/northbound:latest` image, so a normal installation does not build Northbound from source. On a fresh installation, Northbound automatically opens the setup wizard to create the first Platform Owner. The SQLite database and uploaded media persist across image pulls and container recreation.

This same container can run locally, on a LAN, on a VPS, or behind Nginx Proxy Manager, Cloudflare Tunnel, Caddy, Traefik, or another external proxy. Platform services such as Railway or Render can use the image with an externally managed PostgreSQL database and persistent media storage.

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

For the deterministic development-only showcase dataset and reset-safe seeding command, see [docs/DEMO_DATA.md](docs/DEMO_DATA.md).

To build the current checkout in Docker instead of pulling the released image:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

## PostgreSQL

PostgreSQL is optional, not part of the normal self-hosted install. Use `compose.postgres.yaml` when a larger or advanced deployment needs it. See [docs/SELF_HOSTING.md](docs/SELF_HOSTING.md).
