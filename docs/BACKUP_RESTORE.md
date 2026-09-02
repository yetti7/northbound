# Backup, restore and recovery

This is the canonical recovery procedure for Northbound. See [Self-hosting](SELF_HOSTING.md)
for deployment/configuration and [Hardcover](HARDCOVER.md) for integration setup.
Restoring replaces application state; plan downtime, verify the exact target, and
keep an independent pre-restore backup before proceeding.

## What to protect

- **SQLite:** database and matching uploaded media; default paths are
  `/data/northbound.sqlite3` and `/data/media/`.
- **PostgreSQL:** a native database backup plus matching `/data/media/`.
- **Separately:** `.env`, original Django/encryption keys, deployment configuration,
  and the exact image reference associated with each recovery point.

The built-in SQLite ZIP contains a consistent database copy made with SQLite's
backup API, uploaded media and `northbound-backup.json` metadata. It is not a full
volume archive or configuration backup. It does not package deployment `.env` or
the installation keys. It does include encrypted credentials and sensitive account/
reading data from the database. Keep secrets out of the media directory too.

Media is copied separately from the database snapshot; use a quiet maintenance
window when a coordinated database/media recovery point is important. Do not copy
only the live SQLite file while Northbound is writing to it.

Keep downloaded/native backups outside the Git checkout and off the application
volume, in access-controlled storage. Git/Docker exclusions help prevent mistakes
but are not security controls. Test recovery on a disposable installation with no
automatic provider work before relying on a backup.

## Built-in SQLite backups

As a Platform Owner, open **Platform Administration → Settings → Backups**
(`/config/settings/backups/`).

1. Set automatic backup enablement, weekdays, time and retained count. Times use
   the Platform timezone from General Settings. Review the displayed next run,
   last success, latest failure and stored space use.
2. Select **Create Stored Backup** to make a manual ZIP. This stores the archive;
   it does not automatically download it.
3. In **Stored Backups**, use **Download** and verify the file reaches your separate
   protected backup storage. The UI also provides explicit Restore and Delete actions.

With standard SQLite paths, archives live in `/data/backups/`; custom SQLite paths
place this directory beside the database. Retention removes only older automatic
backups, not manual backups. Monitor disk space: backups and pre-restore copies can
consume the same volume as the application. A local stored backup is not disaster
recovery protection against losing that volume.

The automatic scheduler is already included in the container. There is no backup
schedule environment variable to configure; use the Platform settings UI.
PostgreSQL installations must use native tools instead.

## Restore a Northbound SQLite ZIP

Use a ZIP produced by Northbound, not an arbitrary SQL dump or tar archive. Preserve
the original keys, confirm the correct installation and allow room for a pre-restore
copy of the current database and media.

1. Stop user activity and retain an independent backup of the current state.
2. For a stored backup, select **Restore**, enter your current Platform Owner
   password and the exact confirmation `RESTORE`.
3. Alternatively, choose **Restore from Upload → Validate and Stage Restore**.
   Staging schedules replacement on the next startup; it is not a harmless preview.
   In the standard container, **Restart and Restore** requires the current password
   and `RESTORE` confirmation.
4. The standard Compose configuration enables controlled web restart. If that is
   unavailable, deliberately restart the application after staging:

   ```sh
   docker compose restart web
   ```

5. Startup applies the staged restore before web/worker processes, runs migrations,
   then quarantines restored Hardcover work. If validation, migration or safeguard
   fails, resolve the error before starting workers by another route.
6. Verify `/health/`, sign in using an account/password present in the restored
   database, and check Groups, reading records, media, backup settings and sync status.
   A fresh replacement installation can use temporary first-owner setup to access
   the upload UI, but the restore replaces that temporary account with backup state.

The restore writes `/data/pre-restore-<timestamp>/` with the previous database and
media before replacement. This is a recovery copy, not automatic rollback and not
an off-host backup. Restore markers are operational records: do not remove them to
bypass synchronization safeguards. A ZIP restore also replaces settings and history
with their snapshot values; changes made after the backup are not recovered.

## External restores

These steps apply to an externally restored SQLite file/volume, a native PostgreSQL
restore, or restoration of a pre-restore copy outside the built-in ZIP workflow.
An old marker in a restored volume is **not** evidence that current restored work is safe.

1. Stop Northbound and all its workers. For default SQLite:

   ```sh
   docker compose stop web
   ```

   For the bundled PostgreSQL configuration:

   ```sh
   docker compose -f compose.yaml -f compose.postgres.yaml stop web
   ```

2. Verify the exact database/volume target. Preserve current data and restore the
   chosen database and matching media using your verified volume/native database
   procedure. Restore the original keys and correct configuration. Do not start
   the normal application yet. Check that a restored volume does not contain an
   unintended `restore.pending.zip` that would replace data again at startup;
   resolve any staged restore deliberately before proceeding. PostgreSQL must be running for the next commands,
   but the Northbound `web` service must remain stopped.
3. Select a compatible application image and run migrations, then the **mandatory**
   forced Hardcover safeguard against that restored database. These one-off commands
   bypass normal startup and do not launch web servers/schedulers/workers.

   SQLite:

   ```sh
   docker compose run --rm --no-deps --entrypoint python web manage.py migrate --noinput
   docker compose run --rm --no-deps --entrypoint python web manage.py safeguard_restored_hardcover_sync --force
   ```

   Bundled PostgreSQL (with `db` running):

   ```sh
   docker compose -f compose.yaml -f compose.postgres.yaml run --rm --no-deps --entrypoint python web manage.py migrate --noinput
   docker compose -f compose.yaml -f compose.postgres.yaml run --rm --no-deps --entrypoint python web manage.py safeguard_restored_hardcover_sync --force
   ```

   **Stop if either command fails.** Run the safeguard even when restoring a volume
   that contains old restore markers. For non-Compose operation the same commands
   are `python manage.py migrate --noinput` followed by
   `python manage.py safeguard_restored_hardcover_sync --force`, with the restored
   database configuration loaded and all normal application processes stopped.
4. Only after both succeed, use `docker compose up -d web` (include both `-f` files
   for PostgreSQL). Verify health, login, data/media, and quarantined sync status.

Native PostgreSQL dumps/restores and database/media coordination are operator-managed.
The SQLite ZIP UI cannot restore PostgreSQL. Changing database settings is not a
conversion or restore procedure. No universal destructive SQL/volume replacement
command is supplied here: resolve your target and native backup format first.

## Keys and restored Hardcover work

Use the same `NORTHBOUND_TOKEN_ENCRYPTION_KEY` as the original installation, or the
same `DJANGO_SECRET_KEY` if the dedicated variable was empty. See
[encryption configuration](SELF_HOSTING.md#preserve-the-encryption-key).
Losing the key means saved credentials may need replacement/reconnection; a new key
does not decrypt the old ciphertext. Protect backups and keys separately.

A database snapshot cannot undo provider writes or token rotations that happened
later. Even with the correct key, restored Reader OAuth authorization is invalidated
and requires reconnect. Pending, retrying, processing and already-blocked sync work
is held for reconciliation. Recorded successful/skipped/permanent outcomes and
provider mappings remain intact. The safeguard does not grant new consent;
preference values come from the restored snapshot and should be reviewed.

This prevents a restore from blindly replaying external Hardcover writes. Reader
reconnect and **Sync Now** do not release ambiguous or restore-quarantined work.
Do not clear these holds through bulk SQL/status updates. Preserve evidence and
compare provider records before any supported correction; unprovable outcomes stay
blocked. V1 has no automatic historical reconstruction or backfill feature.

## Rollback and failed upgrades

An older image may not understand a newer database schema. Do not assume image
rollback, reverse migrations or a pre-restore directory are automatic recovery.

Keep the application stopped when recovering. Select an image compatible with the
chosen pre-upgrade backup; preserve failed-upgrade data for investigation, restore
the database and matching media, and use the external-restore safeguards above before
restarting. If the older image lacks those safeguards, do not start its workers
against restored credentials—resolve a safe, compatible recovery plan first.

Northbound cannot roll back Hardcover itself. Restoring Northbound loses local
changes after the snapshot while remote writes may still exist. Verify local data,
owner access, keys, media and integration status before reopening the service.
