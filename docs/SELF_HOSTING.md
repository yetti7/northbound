# Self-hosting Northbound

## Default installation

Northbound's normal self-hosted deployment is one application container using SQLite. The database and uploaded media share one persistent Docker volume mounted at `/data`.

```bash
cp .env.example .env
```

Generate a secret with `openssl rand -hex 32` and use it for `DJANGO_SECRET_KEY`. Set `NORTHBOUND_URL` to the address people will use and, if needed, change `NORTHBOUND_PORT`. Then start Northbound:

```bash
docker compose pull
docker compose up -d
docker compose ps
```

Open `/setup/` at the configured URL. No PostgreSQL administration or database password is required.

The application container automatically runs Challenge schedule processing alongside Gunicorn. Due registration and lifecycle actions are evaluated every 30 seconds on both SQLite and PostgreSQL. SQLite automatic backups use a separate SQLite-specific scheduler process.

## Persistent data

The `northbound_data` volume contains:

```text
/data/northbound.sqlite3
/data/media/
```

`docker compose down` preserves this volume. Do not use `docker compose down --volumes` unless you intend to delete all Northbound data and uploads.

SQLite is intended for a single Northbound container on local storage. Use PostgreSQL instead for multiple application replicas or a database on another host.

## LAN and proxy access

For LAN access, set the address people will use:

```dotenv
NORTHBOUND_URL=http://192.168.1.20:8000
```

See [REVERSE_PROXY.md](REVERSE_PROXY.md) before enabling a public HTTPS domain or Cloudflare Tunnel.

## Upgrades

Back up the data volume, then pull and recreate the application:

```bash
git pull --ff-only
docker compose pull
docker compose up -d
docker compose ps
```

Database migrations run automatically before Gunicorn starts.

## Backups

For a simple consistent backup, briefly stop Northbound and archive the complete data volume:

```bash
docker compose stop
docker run --rm -v northbound_northbound_data:/source:ro -v "$PWD":/backup alpine tar -czf /backup/northbound-data.tar.gz -C /source .
docker compose start
```

The archive contains both SQLite and uploaded media. Test restoration periodically. Do not copy only the live SQLite file while the application is writing to it.

## PostgreSQL option

PostgreSQL remains supported for hosted platforms, multiple application replicas, larger installations, and operators who prefer it. Add `POSTGRES_PASSWORD` to `.env`, then apply the optional override:

```bash
docker compose -f compose.yaml -f compose.postgres.yaml pull
docker compose -f compose.yaml -f compose.postgres.yaml up -d
```

An externally managed PostgreSQL database can instead be configured with `DATABASE_URL`.

## Build from source

Developers can build the current checkout explicitly:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```
