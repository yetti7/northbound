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
