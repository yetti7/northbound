# v1.1.1 controlled release plan

Phase A preparation only. Commands below are for a later separately authorized maintenance window. Do not execute against production during preparation. Canonical recovery: [BACKUP_RESTORE.md](BACKUP_RESTORE.md).

## Baseline and candidate

Verified November 15, 2026: production/main/tag v1.1.0 is `a9e6d8896704db9a8e2b244a2c0f5fefc637438b`; Dev candidate runtime is `8fa022f96b15fd2cb1a1bf67705015a27c5c19d8`. Merge base is `f5c22412df0343556b0e8f68fa2daf48f438e38d`. Dev has 18 additional commits; four production-only commits are merges whose aggregate content delta from the merge base is empty. No production content is missing. Local main is stale and must not define the release base. Existing untracked `scripts/` is excluded.

A separately authorized normal merge from Dev into current remote main preserves both histories; do not reset/rebase/force-push main. Documentation commits change the candidate SHA; PR CI must validate the final merge candidate against current main, not merely the original runtime SHA.

## Validation and publication gates

`container.yml` validates main-targeting PRs: complete maintained Django suite, Personal TBR JavaScript helpers, PostgreSQL 17 checks/migration detection/migrate, SQLite checks/migrate, Compose validation and non-publishing amd64 image build. PR validation does not publish. Main pushes and `v*` tags publish after those dependencies; stable tags and main promote `latest`. Publishing platforms are linux/amd64 and linux/arm64. Dispatch on Dev skips validation and is not a substitute. The separate Dev workflow is manual publication, not a production validation route.

Do not merge solely to trigger CI: main merge itself triggers image publication. Obtain separate merge/publication authorization. After the final approved merge, separately authorize tag/release publication. Record the successful main/tag run, source revision, GHCR index digest, architecture manifests and image version. Verify the actual registry artifact matches the release commit and both platforms; a GitHub tag or release alone is insufficient. Pin `ghcr.io/yetti7/northbound@sha256:<verified-v1.1.1-index>` in production. No production image is published during Phase A.

## Proposed startup change

Production currently layers `/srv/docker/northbound/compose.yaml` then `/srv/docker/northbound/compose.tbr-title-2647958.json`, with project `northbound`, service `web`, data bind `/srv/appdata/northbound:/data` and LAN port 8060. Revalidate those identities on deployment day.

The old override replaces the command with a shell-generated copy of `deploy/start.sh` deleting `restore_pending_backup`, `migrate --noinput` and `safeguard_restored_hardcover_sync`. Retire that command override only; preserve any other reviewed settings it contains. Do not drop the whole layer blindly. Prepare a private replacement override preserving the bind, port, environment, identity, restart policy and all approved settings; pin the new image and set `command: ["./deploy/start.sh"]`.

Normal startup uses `set -eu`, then restore-pending → migrate → restore safeguard → backup scheduler → Challenge scheduler → Hardcover worker → Gunicorn. No worker/web starts if a preceding command fails. Keep a single web container; do not scale it because each instance launches bundled schedulers. Migrations run on every normal restart, with already-applied operations becoming no-ops. Never run concurrent migration jobs. If one-off migration is used for controlled verification, wait for it before normal startup; startup will repeat only the no-op migration check. Never bypass a failed migration or safeguard.

Keep the existing single Challenge scheduler; it also evaluates progress/checkpoints and legacy digests. The bundled child is not independently supervised and HTTP health does not establish scheduler health. Inspect process count and intended next lifecycle milestone after startup. Preserve existing backup schedule and Hardcover policy; do not start extra workers.

Explicit release-only environment:

```dotenv
NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=false
NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=false
NORTHBOUND_DISCORD_EVENTS_ENABLED=false
NORTHBOUND_DISCORD_SCHEDULER_ENABLED=false
```

Keep legacy Group delivery disabled. Do not start `run_discord_events` or `run_discord_scheduler`. Do not copy Dev destinations or preferences. Preserve the original `DJANGO_SECRET_KEY`; production currently uses it as the encryption fallback with no dedicated key. Introducing a new encryption key is not a transparent upgrade.

## Deployment-day recovery point

1. Confirm approval, maintenance window, final CI and immutable image, available disk space, and off-host access-controlled recovery storage. Record exact active Compose file order, current command, image IDs/digests, mount identity, UID/GID, env-file sources and scheduler settings privately. Back up configuration and original keys with mode 0600; never print resolved Compose secrets.
2. Put the existing reverse proxy in maintenance mode using its separately reviewed host-specific procedure, blocking new user requests. Northbound has no documented maintenance switch; do not invent an environment flag. Verify no alternative ingress permits writes. Proxy configuration has not been assessed here; approve its exact maintenance/restore action before execution.
3. Stop `web` using the verified current Compose layers (`docker compose -p northbound --project-directory /srv/docker/northbound -f /srv/docker/northbound/compose.yaml -f /srv/docker/northbound/compose.tbr-title-2647958.json stop web`). Confirm all bundled children exited, no other database writers and no dedicated Discord workers. Do not stop unrelated services.
4. With all writers stopped, preserve the entire production data directory into protected recovery storage, including SQLite database and any sidecars, media and operational restore markers. Use a consistent SQLite backup-API snapshot for the database, not an active raw-file copy. Pair it with media from the same quiet window. Preserve original permissions; checksum both data and separately protected configuration. Record the cutoff time and representative record/media/scoring baselines without personal details.
5. Verify archive readability/CRC, file inventory and checksums; SQLite `integrity_check=ok` and zero foreign-key violations on the recovery copy. Copy off-host and verify hashes. Archive verification and database integrity are separate from application restore verification.
6. Restore a duplicate into a disposable isolated installation with outbound traffic blocked and all worker/integration gates off. Use the exact rollback image and original keys only if required; never mount live production storage. Verify health, owner login, Groups, submissions, media and representative totals. This is application restore verification; a readable ZIP does not prove it.

Existing rollback image: `ghcr.io/yetti7/northbound@sha256:ea7eaae7b8874f3419953404ab1744811d4ea50ed5b1bb0ca8f585a56cefc858`, OCI revision `a9e6d889`, version v1.1.0; local image ID `sha256:fb27871f1d4afd143613a45bf4ad6ea639e3595df8df2b9553b6f94c75b05dd3`. Reverify availability before changing anything. Existing November 14 archives predate newer media and are not the deployment-day recovery point.

## Ordered deployment checklist — separate authorization required

1. Complete recovery steps above before any schema/configuration change. Keep maintenance active and all writers stopped.
2. Validate the privately reviewed replacement Compose configuration: pinned v1.1.1 image, normal startup command, same production storage/network/keys, all Discord gates false, no Dev/live-source settings and no extra services. Preserve the retired original override for evidence, not as the future startup command.
3. Apply the approved configuration with `up -d --no-deps --pull never web` using its exact reviewed file list. Startup serially restores any deliberately staged backup, migrates 0078→0082, applies the safeguard and only then starts bundled workers/web. There must be no unexpected pending restore marker. Stop immediately on startup failure; never run Gunicorn directly to bypass it.
4. Verify all five migrations recorded, SQLite integrity/FKs, original record counts and evidence, representative Reader/Team scores, media references and file hashes. No matching/reconciliation/backfill command is part of deployment.
5. Verify HTTP 200 `/health/`, authentication and Group access; Challenge visibility; all Submission Management sections; Reader Pending edit eligibility; signed staff correction and BOTM/TBR controls; existing Void availability; Team Books, full titles, links and uploaded media. Use read-only checks first; any destructive acceptance POST requires approved disposable fixtures.
6. Confirm one Challenge scheduler, one existing backup scheduler and intended Hardcover worker, correct startup order, no duplicate processors, and no dedicated Discord workers. Confirm global flags and legacy local preferences disabled, and no delivery occurred.
7. Continue only if schema/data/media/scoring and authorized access match expectations. Pause with maintenance retained for ambiguous results, missing evidence or a failed noncritical check. Roll back for migration/integrity failure, unintended record/scoring changes, authentication/privacy regression, wrong image/storage or inability to establish healthy startup safely.
8. Obtain acceptance before reopening ingress. The first newly accepted write after the snapshot is the point after which restoring that database loses new work. Record that boundary explicitly; avoid broad writes before the rollback decision.

## Rollback checklist

1. Retain maintenance and stop all application/integration writers. Preserve failed candidate database, outbox/receipt history, logs, media and configuration privately before replacing anything.
2. Restore the compatible pre-upgrade database plus original keys/configuration and the pinned v1.1.0 image. Image-only rollback is unsafe: added NOT NULL columns without persistent SQL defaults can reject older inserts, and older matching code does not understand new Reader-request evidence. Do not reverse migrations as a recovery shortcut; inverse operations discard new preferences/receipts/artwork references.
3. Restore paired media if referenced files are missing; otherwise preserve existing files, including post-upgrade uploads, without pruning. Do not assume media is interchangeable across snapshots.
4. Use a reviewed rollback configuration with normal startup command, not the obsolete migration-skipping command. Keep web stopped. Using those exact rollback Compose files, run one-offs: `run --rm --no-deps --entrypoint python web manage.py migrate --noinput`, then `run --rm --no-deps --entrypoint python web manage.py safeguard_restored_hardcover_sync --force`. Both must succeed before any workers start. Deliberately resolve unexpected pending-restore markers first.
5. Start the compatible application normally. Repeat integrity, health, owner login, access, representative records/scores, media and process checks before reopening. Verify restored Hardcover work is quarantined; do not bulk-release holds. A restored database cannot undo provider mutations or token rotations; preserve external evidence for reconciliation.
6. Keep Discord off. Restoring receipts can erase evidence of messages sent after the snapshot; preserve candidate receipts and review remote outcomes before any future activation. Never clear queues/history to force redelivery.

## Separate future Discord activation checklist

After separate approval: establish production destinations and intended public/private audiences without exposing secrets; review Group categories/preferences, explicit non-Draft Challenge opt-ins, report slots/timezones and legacy-digest overlap. Inspect pending queues before enabling global gates; local preferences can capture events while global delivery is off. Review privacy with real intended audiences; basic metadata approval grants no anonymous page access or participation/results disclosure.

Use dedicated services on the same pinned image, database filesystem and original configuration, direct Python commands `manage.py run_discord_events --interval 30` and `manage.py run_discord_scheduler --interval 30`, no published ports, independent restart policies and no bundled startup script. Start only after migrations/web health and explicit activation approval. Verify recent heartbeats, receipts and queue behavior. Never-attempted events and report slots older than 90 seconds skip rather than backfill. Retryable 429/5xx outcomes have bounded retries; ambiguous network outcomes are not blindly retried, and 5xx retries may duplicate remote acceptance. Emergency disable: stop dedicated workers first, disable global gates and relevant preferences, reload affected processes; environment edits alone do not change running workers. Preserve all delivery evidence. Stage 2 is not implemented or activated by this plan.

## Deferred Discord interface observation

Post-release usability follow-up only: Group administrators find Delivery Status confusing when it combines global gates, worker heartbeats, recurring-service activation, event diagnostics and legacy hourly digest information. Consider simple delivery states and a clear explanation when the platform administrator has paused delivery, with technical diagnostics under Advanced Settings. Preserve the distinction between saved preferences and actual activation. No UI change is included in v1.1.1 preparation.
