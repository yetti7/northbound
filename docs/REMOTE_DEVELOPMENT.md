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
