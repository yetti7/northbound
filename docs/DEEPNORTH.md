# DeepNorth deployment

DeepNorth currently uses a legacy three-container deployment:

- PostgreSQL
- Northbound with Gunicorn
- an internal Nginx media gateway

Its persistent data remains at:

- `/srv/appdata/northbound/postgres`
- `/srv/appdata/northbound/media`

The gateway publishes `192.168.0.11:8060`, and Nginx Proxy Manager forwards `northbound.deepnorth.app` to that address.

Do not run the default `compose.yaml` against the existing DeepNorth project yet. Docker named volumes would be different storage locations and would make the existing application data appear missing.

Until the portable deployment has been verified and a migration is scheduled, continue using:

```bash
docker compose -f compose.production.yaml pull
docker compose -f compose.production.yaml up -d
```

Back up both persistent directories before every application upgrade. The later migration should retain these exact host paths, remove only the gateway layer, verify existing profile-picture URLs, and include a tested rollback to the three-container configuration.
