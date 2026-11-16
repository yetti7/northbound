# Northbound v1.1.2a deployment preparation

The Production transition below is unapplied. Release publication does not
authorize Production deployment; obtain separate deployment approval. Do not use
historical iCloud backups as build inputs or modify the independent disk recovery.

## Runtime contract

`deploy/start.sh` performs the existing initialization exactly once, then execs
`deploy/supervise.py`. The supervisor starts one Gunicorn master (with the
existing configured Gunicorn worker count) and one process for each enabled
background role. It never starts from Django AppConfig or a Gunicorn hook.

The three original background roles default on; both Discord roles default off
and retain their independent environment gates. Existing transport checks remain
the authority for global delivery, Development allowance and destination/Challenge
consent. Worker command defaults remain 30 seconds; Discord arguments explicitly
retain `--interval 30`. PostgreSQL omits the unsupported SQLite backup daemon.

Production should use `init: true`, `stop_grace_period: 45s` and its existing
`restart: unless-stopped`. Supervisor shutdown defaults to 30 seconds; if setting
`NORTHBOUND_SHUTDOWN_TIMEOUT`, keep Compose's grace period longer than that timeout
plus up to one second per child for final reaping. No third-party supervisor,
database schema change or background-job mechanism is introduced.

The HTTP health check stays independent of external providers. A dead child
causes the supervisor to fail; health alone is not proof that a live worker is
making progress. Inspect existing worker heartbeat diagnostics as well.

## Production candidate

A proposed single-service Compose file is retained on deepnorth at:

`/srv/docker/northbound-dev/recovery/v112a-supervisor-20261116T070737Z/production-compose.proposed.yaml`

The matching original three-service file is retained beside it as
`production-compose.v1.1.2.before.yaml`. Recheck that Production has not changed
since preparation before using either file.
It is generated from the actual Production raw Compose file on that host; raw
environment values are not exported. Validation compares the effective web
configuration with the existing one: only image, init and stop grace change;
the two worker service definitions are removed. Ports, networks, mounts, restart
policy, environment and HTTP health check remain identical.

The proposal selects `ghcr.io/yetti7/northbound:v1.1.2a`. After authorized
publication, verify the image revision, manifest architectures and digest before
deployment. Prefer recording the verified digest in the standard Compose file.
Future service updates still use ordinary `docker compose pull` and
`docker compose up -d`, with the approved release selected in that one file.
No extra Production overrides, worker commands or post-update scripts are needed.

## Future authorized transition

1. Publish/verify the approved image separately. Pre-pull it before interrupting
   workers. Set the reviewed paths and validate the candidate:

   ```sh
   NB_V112A_CANDIDATE=/srv/docker/northbound-dev/recovery/v112a-supervisor-20261116T070737Z/production-compose.proposed.yaml
   NB_V112A_ROLLBACK=/srv/docker/northbound-dev/recovery/v112a-supervisor-20261116T070737Z/production-compose.v1.1.2.before.yaml
   cd /srv/docker/northbound
   docker pull ghcr.io/yetti7/northbound:v1.1.2a
   docker compose --project-directory /srv/docker/northbound -f "$NB_V112A_CANDIDATE" config --quiet
   ```

   Inspect the effective config privately; it must contain only `web`, with the
   approved new image. Verify image revision/version before the following steps.
2. Save the current three-service `compose.yaml` to a protected rollback path;
   verify its image is the approved v1.1.2 digest below. Record current consent,
   schedules, encrypted destination configuration and delivery diagnostics
   privately for comparison. Do not modify their records.
3. In `/srv/docker/northbound`, run:

   ```sh
   cd /srv/docker/northbound
   docker compose stop discord-events discord-scheduler
   ```

   Verify both named old containers are stopped. Do not rely on deduplication to
   justify overlap. Do not start the consolidated image before this check passes.
4. Install the reviewed candidate as `/srv/docker/northbound/compose.yaml`, then:

   ```sh
   cp "$NB_V112A_CANDIDATE" /srv/docker/northbound/compose.yaml
   docker compose pull
   docker compose up -d --no-deps web
   ```

5. Verify `/health/` HTTP 200, one supervisor, one Gunicorn master, one instance of
   each enabled management command, fresh `discord`/`discord-events` daemon
   heartbeats and no unexpected restart loop. Compare consent/configuration and
   sanitized delivery outcomes. Do not send test notifications. Inspect event
   keys/report slots for duplicate delivery evidence; do not reset claims.
6. After acceptance, remove only the two stopped old worker container objects
   (`docker rm northbound-discord-events northbound-discord-scheduler`), with no
   force or volume-removal flags. This step also requires deployment approval.
   Confirm ordinary Compose commands recognize only the consolidated web service.

Keep the interruption short: existing event/report missed-window rules continue
to apply. Pull and validate before stopping. The supervisor does not add catch-up,
retry or exactly-once semantics; interrupted external requests can remain uncertain.

## Rollback

The prior image is:

`ghcr.io/yetti7/northbound@sha256:01d97fedf7abc2290ed14548ae13a69bccd98f043a6e96f3eb0f07881c163b12`

Stop the consolidated web service first and verify its process groups have exited.
Only then restore the protected original three-service Compose file and run:

```sh
cd /srv/docker/northbound
docker compose stop -t 45 web
# Verify the consolidated container is stopped before restoring old workers.
cp "$NB_V112A_ROLLBACK" /srv/docker/northbound/compose.yaml
docker compose up -d --no-deps web
# After web health is confirmed:
docker compose up -d --no-deps discord-events discord-scheduler
```

The stopped worker containers can be recreated from their existing service
definitions if already removed. Preserve the same database/media mounts and all
delivery records. There is no schema change in this correction, so configuration
and image rollback require no database restore or manual migrations. Do not
restore an older database over live submissions or discard notification receipts.

## Development

Deploy only a locally built Development image, not a published release. Preserve
the existing source mounts, all delivery gates, SMTP configuration and data.
The live-source override uses the supervisor with Gunicorn's existing reload
options supplied through `GUNICORN_CMD_ARGS`. Existing Development automatic
workers remain disabled; fake-child tests cover their lifecycle without enabling
live Discord. Keep rollback configs and a consistent SQLite snapshot protected
on deepnorth, outside the Docker build context. Do not access iCloud backups.
