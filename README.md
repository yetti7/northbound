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

Northbound's default deployment includes the application, PostgreSQL, and persistent named volumes. It does not require Nginx or another reverse proxy for local or LAN use.

```bash
cp .env.example .env
```

Edit `.env` and replace `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD` with different random values. For example, `openssl rand -hex 32` can generate each value. Then pull and start Northbound:

```bash
docker compose pull
docker compose up -d
```

Compose pulls the published `ghcr.io/yetti7/northbound:latest` image, so a normal installation does not build Northbound from source. Open <http://localhost:8000/setup/> to create the first administrator account. Uploaded profile pictures and PostgreSQL data persist across container recreation.

See [docs/SELF_HOSTING.md](docs/SELF_HOSTING.md) for LAN access, upgrades, backups, and external databases. See [docs/REVERSE_PROXY.md](docs/REVERSE_PROXY.md) before placing Northbound behind HTTPS, Cloudflare Tunnel, Nginx, Caddy, Traefik, or another proxy.

## Local Python development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Without `DATABASE_URL` or `POSTGRES_HOST`, local development uses SQLite. This convenience does not change the recommended PostgreSQL production deployment.

To build the current checkout in Docker instead of pulling the released image:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

## Existing DeepNorth installation

The current DeepNorth deployment remains available through `compose.production.yaml` and continues using `/srv/appdata/northbound`. Do not replace that running stack with the default named-volume deployment. Its migration will be handled separately after the portable deployment is verified. See [docs/DEEPNORTH.md](docs/DEEPNORTH.md).
