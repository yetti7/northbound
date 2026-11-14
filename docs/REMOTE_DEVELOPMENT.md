# Northbound remote development from Codex on macOS

## Verified connection and boundaries

- SSH: `taylor@192.168.0.11` (deepnorth).
- Dedicated Mac key: `~/.ssh/northbound-dev-codex`; passphrase stored through macOS Keychain/ssh-agent. The private key stays on the Mac and is not in Git.
- Only its public key was appended remotely with `restrict`; existing authorized-key entries and SSH daemon configuration were preserved.
- Development Compose: `/srv/docker/northbound-dev/compose.yaml`, project `northbound-dev`, service `web`, container `northbound-dev`.
- Image: `ghcr.io/yetti7/northbound:dev`; development data: `/srv/appdata/northbound-dev:/data`.
- Development URL: `http://192.168.0.11:8061`; health: `/health/`.

Production Northbound, port 8060, and every other Docker service are off-limits.
Do not deploy, pull, or restart without explicit task authorization. Do not print
`.env`, unfiltered `docker inspect`, or resolved Compose environment values.

The key inherits the existing taylor account's permissions. `restrict` disables
SSH forwarding and PTY features but does not enforce development-only commands.
A server-side forced-command wrapper or dedicated restricted account requires
separate approval; neither was installed as part of this setup.

## SSH command

Use this shell function in the Codex command session; no global SSH configuration
or remote Codex CLI installation is needed:

```sh
nbdev() {
  ssh -S none -T -i "$HOME/.ssh/northbound-dev-codex" \
    -o IdentitiesOnly=yes -o UseKeychain=yes -o BatchMode=yes \
    -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
    -o StrictHostKeyChecking=yes -o ConnectTimeout=8 \
    taylor@192.168.0.11 "$@"
}
```

If the key is locked, run `ssh-add --apple-use-keychain ~/.ssh/northbound-dev-codex`
interactively in Terminal. Never send passwords/passphrases through chat.

## Read-only inspection

```sh
nbdev 'docker compose -p northbound-dev --project-directory /srv/docker/northbound-dev -f /srv/docker/northbound-dev/compose.yaml ps web'
nbdev 'docker inspect --format "{{.State.Status}}" northbound-dev'
nbdev 'docker compose -p northbound-dev --project-directory /srv/docker/northbound-dev -f /srv/docker/northbound-dev/compose.yaml config --quiet'
nbdev 'docker compose -p northbound-dev --project-directory /srv/docker/northbound-dev -f /srv/docker/northbound-dev/compose.yaml config --services'
nbdev 'curl --fail --silent --output /dev/null http://192.168.0.11:8061/health/'
```

Inspect logs from only `docker logs --tail 100 northbound-dev`. Capture logs
privately, redact credentials/payloads before displaying excerpts, and report
summaries or safe exception locations instead of raw dumps. Do not inspect other
containers to troubleshoot development. If Docker reports a permission error,
stop rather than adding Docker-group membership or broad sudo access.

## Updating development — only after explicit approval

First verify the target still has Compose project `northbound-dev`, only the
expected development data mount, and the expected dev image. Record the current
image ID/digest and take a development-only consistent SQLite backup before
updates that can run migrations. Do not read or back up production data.

Pull only the development service:

```sh
nbdev 'docker compose -p northbound-dev --project-directory /srv/docker/northbound-dev -f /srv/docker/northbound-dev/compose.yaml pull web'
```

Apply the pulled image:

```sh
nbdev 'docker compose -p northbound-dev --project-directory /srv/docker/northbound-dev -f /srv/docker/northbound-dev/compose.yaml up -d --no-deps web'
```

Restart the existing development container without pulling a new image:

```sh
nbdev 'docker compose -p northbound-dev --project-directory /srv/docker/northbound-dev -f /srv/docker/northbound-dev/compose.yaml restart web'
```

Recheck status, health, safe logs, and actual image revision after an authorized
update. Startup can apply migrations; image-only rollback may require restoring
the matching development database. Never use host-wide prune, arbitrary Compose
paths, production commands, `down -v`, or a remote Docker context with unrestricted
operations.

## Initial verification findings

Dedicated-key authentication, development status/log access, and Compose validation
passed. The inspected configuration uses `/data/northbound.sqlite3`, `/data/media`,
no external database override, separate development cookie names, DEBUG off,
DEVELOPMENT banner on, and backup/Challenge schedulers and Hardcover write-back off.

The port is bound to the LAN address `192.168.0.11:8061`, not Tailscale-only. No
Docker healthcheck is configured. Changing network exposure or adding a healthcheck
requires approval; no remote Compose changes or deployment were performed.


## Clean local working-tree Dev builds

For authorized local-image deployments, use `Dockerfile.dev` with a private prepared
context containing the **complete approved runtime tree** under `app/`. Do not use
`FROM northbound:dev-local-...` or apply successive file overlays to the last Dev
image: that accumulated 127 layers and exhausted Docker's layer-depth limit.
The production Dockerfile and release workflow are unchanged.

The Dev Dockerfile pins the cached dependency foundation by immutable digest:
`ghcr.io/yetti7/northbound@sha256:71b03b2446270d51e031011dbabe6d8affe05de77b8430c8cc1acc34b9c277ad`.
Its local image ID is `sha256:a9d3b9953ee49f000ee3a0795033c4c45c993e263fab2cfabeb97dea37c0bd55`
and it has 11 layers. At recovery verification its OS/Python layers, Python 3.13.18,
all installed Python-package versions/content, and system-package inventory matched
the running Dev runtime. Only the image's `NORTHBOUND_VERSION` default differed;
`Dockerfile.dev` preserves the prior `development` value. Do not replace this pin
with a floating/newer image without separately reviewing dependency changes.
No package installation or upgrade is needed for this build.

Prepare the context from an approved runtime source manifest, preserving unrelated
working-tree files. Exclude databases, media, secrets, unreviewed scripts, fixtures
and documentation. Copy `Dockerfile.dev` into the context as `Dockerfile`.
Keep the context root mode `0700`, child directories `0755`, application files
`0444`, and `app/deploy/start.sh` executable (`0555`). Preserve modes on SSH transfer
(`tar -xpf`); a private extraction umask must not make runtime source unreadable.
On macOS, create transfer archives from explicit file bytes with Python `tarfile`
(`TarInfo`, USTAR format, no copied metadata) or an equivalently metadata-free
mechanism. Verify the archive, extracted context and candidate image contain no
`._*` / `.DS_Store` files; do not rely on Docker ignore rules to remove metadata
created by transfer. Include every approved untracked runtime file in the manifest.

**Required static collection:** The pinned foundation already contains collected
static output. `Dockerfile.dev` guards that `STATIC_ROOT` is image-local
`/app/staticfiles` and is distinct from source `/app/static` and media, then runs
`python manage.py collectstatic --clear --noinput` as the normal application user.
This replaces inherited output regardless of incoming source mtimes. Never clear
source files, media or persistent volumes. Epoch timestamps in a metadata-free
archive are safe only with a fresh collection; they previously caused stale assets
when the inherited output was retained.

Before cutover, verify Django static-finder winner hashes against collected output,
decode/check compressed variants, and compare the **actual HTTP-served bytes** from
the isolated running candidate. The current `CompressedStaticFilesStorage` preserves
uncompressed bytes; if storage changes to transform content, compare its expected
postprocessed output. Check all available static files, including Themes, prompts,
Reader Rumble, branding, navigation and submission editing—not only asset existence.
The successful coordinated deployment checked 171 approved application assets and
298 total Django-selected assets in both disposable preflight and live Dev.

The normal non-root application user runs `collectstatic` during the clean build;
the inherited startup command, UID/GID and persistent `/data` expectations remain.

Build only the authorized Dev image, without pulling or updating dependencies:

```sh
docker build --pull=false --network none --platform linux/amd64 \
  -t northbound:dev-local-<unique-approved-build> <private-prepared-context>
```

Before deployment, verify the layer count, runtime metadata/dependency fingerprints,
full source manifest, read-only source permissions, collected static assets and
Django/migration plan as the normal application user. Start an isolated preflight
container with external integrations disabled and a consistent disposable SQLite
copy; never mount shared Dev data writable for this check. Then follow the existing
Dev-only backup, image-only Compose override, migration, health and data-comparison
procedure. Retain rollback images and private effective configuration. Never flatten
the running image or reset persistent data to solve image layering.

## Opt-in live source (Issue #5)

Live source is deployed on Dev only. DEBUG remains off. The existing image is
`northbound:dev-local-v110staticfix-20261114T034506Z` (ID
`sha256:a876767883b7b7397c51c21acdea5ddc5b99d4ab945a6072a5e5f0e45521743d`).
The initial private configuration backup is
`/srv/docker/northbound-dev/rollback-live-source-20261114T161611Z`.
`live-source-rollback-path` in that directory's parent identifies this backup.
It contains the original Compose files, private effective configuration and
container inspection. Do not print or commit these files: they contain secrets.

The override in `deploy/compose.live-source.yaml` mounts only `core`,
`reading_challenge`, `templates` and `static` from
`/srv/docker/northbound-dev/source` onto their `/app` counterparts, read-only.
The original `/srv/appdata/northbound-dev:/data` mount remains writable and intact.
There are no production, repository-root, secrets, backup or database source mounts.
Gunicorn starts directly with two workers and polling reload; the image startup
script is bypassed, so enabling this mode does not run migrations or restore jobs.
Do not use this override for production.

### Initial setup and enable

Before first setup, inspect Dev's image, mounts, command and Compose config-file
label using the scoped inspection commands above. Privately copy every active
Compose file and save `docker compose ... config` and the container inspection in
a mode-0700 rollback directory. Preserve files as mode 0600. Record the exact image
ID and verify all storage paths are Dev-only. Never replace the original Compose
files. Review the approved runtime source and follow the sync procedure below.

Copy the override (using the `nbdev` function above):

```sh
nbdev 'cat > /srv/docker/northbound-dev/compose.live-source.yaml' < deploy/compose.live-source.yaml
```

For this deployment the existing file list is the following. Keep **all** existing
overrides, especially the pinned local-image override, in this order:

```sh
nblive() {
  nbdev "docker compose -p northbound-dev --project-directory /srv/docker/northbound-dev \
    -f /srv/docker/northbound-dev/compose.yaml \
    -f /srv/docker/northbound-dev/compose.teambranding-20261113T150742Z.yaml \
    -f /srv/docker/northbound-dev/compose.v110staticfix-20261114T034506Z.yaml \
    -f /srv/docker/northbound-dev/compose.live-source.yaml $*"
}
nblive config --quiet
nblive up -d --no-deps --pull never web
```

Before `up`, privately inspect the merged configuration: exactly the four read-only
source mounts plus the existing `/data` mount, same pinned image, development
banner enabled, DEBUG off, backup/Challenge schedulers, Hardcover write-back and
both Discord flags off. Settings also reject live-source activation without the
banner, with DEBUG on, or with Hardcover write-back on.

### Routine source synchronization

Review `git status --short` and `git diff` first. The packager reads **current
working-tree bytes of tracked files**, excluding tests and hidden files; it does
not include untracked files, `scripts/`, secrets, databases, media or collected
static output. Add an approved new runtime file to Git's index before packaging.
Do not add unrelated files. Review dependencies and migrations separately before
syncing; source sync never applies schema changes.

```sh
python3 deploy/package_live_source.py /tmp/northbound-source.tar
# Inspect the exact approved manifest before transfer.
tar -tf /tmp/northbound-source.tar
nbdev 'mkdir -p /srv/docker/northbound-dev/source.next; chmod 700 /srv/docker/northbound-dev/source.next; tar -xpf - -C /srv/docker/northbound-dev/source.next' < /tmp/northbound-source.tar
nbdev 'find /srv/docker/northbound-dev/source.next -type d -exec chmod 755 {} +; mkdir -p /srv/docker/northbound-dev/source; chmod 755 /srv/docker/northbound-dev/source; for package in core reading_challenge templates static; do test -d /srv/docker/northbound-dev/source.next/$package || exit 1; mkdir -p /srv/docker/northbound-dev/source/$package; rsync -rc --delete --omit-dir-times /srv/docker/northbound-dev/source.next/$package/ /srv/docker/northbound-dev/source/$package/ || exit 1; done'
```

Use an empty `source.next` for each transfer (rename a previous staging directory
within the Dev directory before reusing that name). The narrowly scoped `--delete`
removes obsolete runtime files only inside those four source directories; never
point it at `/data`, appdata, a repository root or a production path. `-c` detects
changed bytes even when timestamps match. The archive preserves source mtimes
without macOS metadata. For a multi-file change, or an edit with an unchanged mtime,
restart Dev workers after synchronization to avoid mixed versions:

```sh
nblive restart web
```

Python modules reload automatically when polling detects a changed mtime. Templates
use uncached filesystem/app loaders even with DEBUG=False. WhiteNoise uses Django
source finders and autorefresh with `max-age=0`, so CSS/JS come from source rather
than inherited collected assets. Refresh the browser; use a cache-disabled refresh
when diagnosing a previously cached response. Do not run `collectstatic` for this
workflow. New static/template files appear on request. Settings or package-layout
changes should receive an explicit Dev restart.

### Verify and preserve data

```sh
nbdev 'curl --fail --silent --output /dev/null http://192.168.0.11:8061/health/'
nbdev 'docker exec northbound-dev python manage.py shell -c "from django.conf import settings; assert settings.NORTHBOUND_LIVE_SOURCE; assert not settings.DEBUG; assert settings.NORTHBOUND_DEVELOPMENT_BANNER; assert not settings.NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED; assert not settings.NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED; assert not settings.NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT; assert settings.WHITENOISE_USE_FINDERS and settings.WHITENOISE_AUTOREFRESH"'
nbdev 'docker inspect --format "{{json .Mounts}} {{json .Config.Cmd}}" northbound-dev'
nbdev 'docker top northbound-dev -eo pid,args'
```

Check the DEVELOPMENT banner on the login page and inspect actual HTTP CSS/JS
response bytes. For a focused live-edit check, temporarily append a unique comment
to an existing CSS file and template, request each, then restore both. Verify Python
reload with a harmless temporary response header and restore it; do not modify data
or invoke integrations. Initial deployment passed all three real HTTP probes,
including reload after restoring Python. Database logical-content and every media
file hash matched before/after; health returned 200. No migrations ran. Production
received no commands; its configuration was not independently fingerprinted.
The approved branch also contained an existing navigation mapping change in
`core/context_processors.py` relative to the running image; synchronized source
follows the branch. Test files absent from the runtime image are not synchronized.

For future deployments, record the Dev database's read-only logical-content digest
and media hashes before/after in private files, or compare application row counts
and media hashes if concurrent authorized Dev use makes identical digests
impractical. Confirm `/data` mount identity, existing records and uploaded media.
Never clear or replace persistent storage as part of source synchronization.

### Rebuild boundaries and rollback

Rebuild the Dev image for Python/system dependencies, Dockerfile/build-time changes,
or other image-only content. Apply database schema changes only with separate
explicit migration authorization and an appropriate consistent Dev database backup.
An image rebuild may run startup migrations unless its command remains overridden.

To disable live source and restore the exact pre-task configuration, use the
private effective backup, which preserves the original image, command and mounts:

```sh
nbdev 'docker compose -p northbound-dev --project-directory /srv/docker/northbound-dev -f /srv/docker/northbound-dev/rollback-live-source-20261114T161611Z/effective.yaml config --quiet'
nbdev 'docker compose -p northbound-dev --project-directory /srv/docker/northbound-dev -f /srv/docker/northbound-dev/rollback-live-source-20261114T161611Z/effective.yaml up -d --no-deps --pull never web'
```

Recheck health, banner, original `/data` mount, existing records and media. Leave
source, overrides, backup and rollback image available; no data restoration is
needed for this configuration-only deployment. Re-enabling uses `nblive up` above.

### Image publication workflows

`development.yml` now runs only through `workflow_dispatch`; ordinary `dev` pushes
neither build nor publish images. For an authorized manual build, select the `dev`
branch: its existing job guard requires `refs/heads/dev`, and checkout, image version
and `dev-sha-*` tag use that selected ref's SHA. GitHub must have the workflow on the
default branch for its dispatch UI/API availability; this task does not merge it.
The existing default-branch workflow already declares dispatch. No workflow was
manually triggered for this task.

**Pending owner approval — not applied:** in `container.yml`, remove only
`on.push.branches: [main]`, retaining `on.push.tags: ["v*"]`,
`pull_request.branches: [main]` and `workflow_dispatch`. Main pushes would stop
running the production pipeline or publishing images. Release tags would retain
validation and publication, including stable-tag promotion to `latest`; explicit
manual dispatch on main would still validate, publish SHA and `latest` tags.
PR validation and its non-publishing image build remain unchanged. The existing
manual-dispatch-on-dev exclusion also remains. No production workflow file was
modified. This proposal requires separate owner approval before application.
