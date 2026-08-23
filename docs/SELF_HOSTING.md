# Self-hosting Northbound

## Requirements

- Docker Engine with the Docker Compose plugin
- enough storage for PostgreSQL and uploaded profile pictures

## First installation

Clone the repository, enter its directory, and create the local environment file:

```bash
cp .env.example .env
```

Generate two different secrets:

```bash
openssl rand -hex 32
openssl rand -hex 32
```

Use one for `DJANGO_SECRET_KEY` and the other for `POSTGRES_PASSWORD`. Keep `.env` private and backed up. Start the application:

```bash
docker compose up -d --build
docker compose ps
```

Open <http://localhost:8000/setup/> and create the first administrator account.

## LAN access

The default container port is published on all host interfaces. Change `NORTHBOUND_URL` to the address people will use, for example:

```dotenv
NORTHBOUND_URL=http://192.168.1.20:8000
```

Restart the web container after changing settings:

```bash
docker compose up -d
```

Do not expose an unencrypted HTTP installation directly to the internet.

## Persistent data

The default deployment creates two Docker named volumes:

- `northbound_postgres_data` for the database
- `northbound_media_data` for uploaded profile pictures

`docker compose down` preserves these volumes. Do not use `docker compose down --volumes` unless you intentionally want to delete Northbound's persistent data.

## Upgrades

Back up the database and media volume first. Then update the repository and rebuild:

```bash
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
```

Database migrations run automatically before the web process starts. Review release notes before upgrading across major versions.

## Backups

Create a PostgreSQL dump outside the containers:

```bash
docker compose exec -T db pg_dump -U northbound -d northbound > northbound-postgres.sql
```

If you changed `POSTGRES_USER` or `POSTGRES_DB`, use those values in the command.

Back up the media volume separately. One portable approach is:

```bash
docker run --rm -v northbound_media_data:/source:ro -v "$PWD":/backup alpine tar -czf /backup/northbound-media.tar.gz -C /source .
```

A complete backup requires both the PostgreSQL dump and the media archive. Test restoration periodically rather than assuming an untested backup is usable.

## Existing PostgreSQL

Outside Compose, Northbound accepts a conventional `DATABASE_URL`. Hosted PostgreSQL URLs, including supported SSL query parameters, are parsed by `dj-database-url`.

When `DATABASE_URL` is absent, local Python development uses SQLite. PostgreSQL remains the recommended production database.

## Uploaded media

The default single-instance deployment streams uploaded files through Northbound with production debug mode disabled. This mode is intended for profile pictures and similarly small files.

Profile pictures are limited to 10 MB, and Northbound rejects requests whose declared body size exceeds 11 MB. A public reverse proxy should enforce its own request-size limit as an additional boundary.

Large, high-traffic, or horizontally scaled installations should use object storage when that backend becomes available. WhiteNoise serves versioned application static assets; it is not being used as mutable upload storage.
