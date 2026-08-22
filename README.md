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

## Local development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open <http://127.0.0.1:8000/setup/> for the first-run wizard.

## Docker

Copy `.env.example` to `.env`, change the secrets, then run:

```bash
docker compose up --build
```

The site will be available at <http://localhost:8000>.

## DeepNorth Production Deployment

The `main` branch publishes a multi-architecture container image to GitHub Container Registry after the Django checks and tests pass. Because the repository and package are private, authenticate the DeepNorth host before its first pull:

```bash
echo "$GITHUB_CONTAINER_TOKEN" | docker login ghcr.io -u yetti7 --password-stdin
```

Use a classic personal access token with `read:packages` for this login. Keep the token out of `.env`, shell history, and the repository.

On DeepNorth, place `compose.production.yaml` and a private `.env` in `/srv/docker/northbound`. Generate unique production secrets and then deploy:

```bash
cd /srv/docker/northbound
docker compose -f compose.production.yaml pull
docker compose -f compose.production.yaml up -d
docker compose -f compose.production.yaml ps
```

Persistent PostgreSQL and uploaded-media data default to `/srv/appdata/northbound`. The production example publishes container port `8000` at `192.168.0.11:8060` on DeepNorth. It supports both direct LAN access at <http://192.168.0.11:8060> and public access at <https://northbound.deepnorth.app> through Nginx Proxy Manager. Configure the proxy host to forward to `192.168.0.11:8060`, enable HTTPS, and pass the original protocol through `X-Forwarded-Proto`.

Because direct LAN HTTP is intentionally supported, the example disables Django's global HTTPS redirect and the `Secure` attribute on session and CSRF cookies. HTTPS detection behind Nginx Proxy Manager remains enabled through `SECURE_PROXY_SSL_HEADER`. One year of HSTS applies to HTTPS responses for `northbound.deepnorth.app`, without including subdomains or requesting browser preload.

Before the first deployment, create the persistent directories:

```bash
sudo mkdir -p /srv/appdata/northbound/postgres /srv/appdata/northbound/media
```

Let the official PostgreSQL container initialize its empty data directory. If DeepNorth's existing filesystem permissions prevent that initialization, inspect the container's reported UID/GID before changing ownership rather than assuming a value.

The application container runs as UID/GID `10001`. Give that account ownership of the media directory before first start:

```bash
sudo chown -R 10001:10001 /srv/appdata/northbound/media
```

Back up both persistent directories before upgrades. Database migrations run automatically when the web container starts, so take the PostgreSQL backup before pulling and recreating containers.
