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

## Advanced branding preferences (Issue #3)

Migration `core.0078_advanced_branding_preferences` adds independent preferences to
Groups, Challenges and Teams: `gradient_enabled=False`, `gradient_coverage=75` and
`gradient_darkness=85`. Team also has `hide_inherited_logo=False`. Sliders accept
integer percentages from 0 through 100, validated by forms and model validators.
Existing records retain their exact legacy gradients while customization is off;
no artwork, stored display modes, scoring, lifecycle or historical audits change.

With customization enabled, coverage is the percentage of banner width receiving
a left-to-right gradient, fading to transparent at that percentage. Zero coverage
means no overlay; 100 covers the full width. Darkness is opacity at the left edge,
from transparent (0) to opaque (100). This does not crop, reposition or modify an
image. Very low values can reduce text contrast. Preferences belong to the displayed
entity and do not follow inherited artwork. Compact Header still hides banners.
Restore Defaults changes only the three gradient controls (off / 75 / 85); save to
apply. It does not clear artwork or reset Hide Inherited Logo.

Hide Inherited Logo suppresses parent logos only on Team headers, cards and editor
Header Preview; a valid custom Team logo still wins. A Team with its own available
banner, suppression enabled and no available custom logo qualifies for showcase.
Its All Teams card omits all dimming, overriding custom gradients; the name stays
below artwork. Its detail/preview retains a localized readable overlay unless a
custom gradient is enabled. Wrapped and Reader Rumble explicitly retain their
previous logo behavior. Saved-header previews share the actual header templates;
existing local artwork upload/removal previews remain available.

For the authorized development migration, first validate the current Compose stack,
image and mounts, save a consistent SQLite backup using SQLite's backup API, and
review `migrate core 0078 --plan`. Rehearse on a disposable backup copy. Stop only
`northbound-dev` during source/schema cutover, so reloading workers cannot query
columns before they exist. Sync reviewed source, then use the same Dev Compose
file list with `run --rm --no-deps --entrypoint python web manage.py migrate core
0078 --noinput`; this bypasses normal startup. Restart the existing container after
success. Never run these commands against production. Confirm HTTP health, disabled
integrations, new defaults, migration state, unchanged prior-column row digests,
media hashes and database integrity.

Pre-Issue-3 Dev source, configuration and consistent database backup are stored in
`/srv/docker/northbound-dev/rollback-branding3-5389f8d`. Because the migration is
additive, the previous source can run with the extra columns still present: stop
Dev, restore its four source directories from that private `source.tar`, then start
Dev and verify health. Leave the new columns in place for this source-only rollback;
do not reverse the migration or replace the database casually. A database restore
would discard any subsequent Dev changes and requires a separately reviewed plan.

## Discord Stage A: configuration and manual messages

Migration `core.0079_discord_manual_destinations` adds disabled-by-default
`community_enabled` and `moderator_enabled` manual switches and an empty encrypted
Community webhook field. The existing `encrypted_webhook` remains the Moderator
webhook, byte-for-byte. Its existing `enabled` flag and all scheduled digest
fields/history retain their original semantics. The new settings interface changes
only manual switches; saving it cannot turn on the old hourly digest. Existing
legacy scheduling status is displayed separately and is not changed by this work.

Owners and Platform Administrators can independently save, replace, clear, test or
send status to either destination; Moderators remain read-only. Password inputs
never redisplay a submitted or stored URL. Saving, GET requests and migrations
send nothing. Manual Test/Current Status use POST, CSRF, one-hour signed request
tokens bound to actor/Group/destination/action, a unique persistent manual claim,
and redirect-after-POST. Reusing a request cannot resend it, including uncertain
outcomes. A new explicit button activation may send another message.

Manual attempts use `DiscordManualDelivery`, recording destination, action, attempt
time, sanitized outcome/category and any rate-limit cooldown. No URL, key, payload
or provider body is stored. Manual delivery never updates scheduled digest claims,
last/next attempt timestamps or last-sent status. Transport retains exact Discord
HTTPS URL validation, disabled mentions, no redirects, a bounded timeout and no
automatic retry. Manual failure feedback distinguishes missing/disabled destinations,
global/development blocks, unreadable/invalid webhooks, timeouts, rate limits,
rejection, connectivity failures and unavailable snapshots. Failed network requests
can have uncertain delivery; check Discord before making a new request.

Diagnostics distinguish saved configuration from proven delivery and expose the
last manual result and action for each destination. Past results may refer to an
earlier saved URL. Scheduler readiness is separate: a disabled scheduler is shown
as such; when enabled by configuration, process availability remains unverified.
Manual sends do not require the scheduler or a public application URL.

Moderator Current Status uses existing Group-wide review-count services, preserving
Team transfer, retained-credit, eligibility and unassigned-review rules. Rich embeds
group Book, Theme, Immersive, Bingo, Scavenger Hunt Book and Photo tasks by Challenge
and Team, showing only nonzero categories. Totals explicitly count review tasks,
not unique submissions. An empty queue produces one green All Caught Up card.
Community Current Status is a neutral branded reading-community card with Group
identity only; it does not query or disclose Challenge, participation, result,
score or moderation data. Neither snapshot changes lifecycle or review state.

Development's two Discord delivery gates were explicitly enabled for authorized
real manual testing. The Challenge scheduler and all legacy hourly digests remain
disabled. The repository's Compose template still defaults both delivery gates off;
do not overwrite the live host's approved gate values during source synchronization.
Use only the user's existing development destinations for explicitly authorized real
tests. Never copy production webhooks or enable the scheduler for manual testing.
All automated delivery tests use mocked transport.

The consistent pre-Stage-A Dev database, source and private configuration backup is
`/srv/docker/northbound-dev/rollback-discord4a-f66ba27`. Migration rehearsal on a
disposable copy includes synthetic encrypted legacy webhook and digest-history
records and verifies their preservation. Follow the paused-worker live-source
migration procedure above, targeting only `core 0079`. Verify all existing row
values, ciphertext, digest history and media afterwards; only the additive schema,
its migration record and Django's new model permissions/content type are expected.
A source-only rollback can leave these additive fields/tables in place. Do not
restore the database over subsequent Dev changes or reverse migrations casually.

Deferred work: Stage B reporting schedules and category controls; Stage C automatic
lifecycle/moderation event delivery, including privacy and duplicate-prevention
policy. No new automatic reporting or event subscriptions are implemented here.

## Discord Stage A.1: rich embeds

Manual tests use green connection cards; Community Status uses teal; Moderator
pending reviews use amber. Every embed has Group identity, an ISO timestamp and a
Northbound footer. No uploaded assets, external images, new dependencies, model
changes or migrations are needed. The legacy hourly digest formatter is unchanged.

Link fields use `NORTHBOUND_URL` and Django routes. Missing/invalid origins produce
no link. Community cards additionally omit literal private/reserved IP addresses,
local/internal hostnames and malformed origins; the code does not infer a public
URL from the request host. Private Moderator links may use the configured private
origin. Public Challenge/participation/results sharing remains a Stage B policy
decision, and is not inferred from an Owner's private access.

Text budgets conservatively count UTF-16 units, preserving emoji boundaries. Embed
titles/field names/author names are limited to 256, descriptions to 4096, field
values to 1024, fields to 25, and all embed text in a message to 6000; up to ten
embeds share a message. Long display names use a visible ellipsis; review categories
and counts are split, never silently omitted. Pages keep deterministic service
ordering and are numbered. Queue links are fields rather than duplicate embed URLs,
since Discord deduplicates embeds with identical URLs in one message.

One manual action can produce multiple messages for a large queue. It retains one
existing request claim and one outcome record; only completion of every part is a
success. Sending stops on any failure with existing rate-limit/cooldown behavior,
without automatic retries. Earlier parts may have arrived if a later part fails;
check the channel before a new explicit request. Reusing the original request
never resends parts. No scheduled claims or timestamps are changed.

Pre-deployment source/configuration/database backup:
`/srv/docker/northbound-dev/rollback-discord-a1-7c45e48`. Source-only rollback can
restore its four source directories and restart only Dev; there is no schema change.
Do not restore a database over later user changes merely to roll back formatting.

Stage A.1 validation: 65 focused Django tests passed, covering rich payload budgets,
Unicode/long names, many Teams, mixed review categories, task totals, public privacy,
private/public URL handling, one-claim multipart delivery, cooldown and refresh
protection. No model changes or migration were needed. The four authorized real
Dev actions (Community Test/Status, Moderator Test/Status) each sent one embed in
one message and received HTTP 204. Existing records across 63 tables and all 40
media files were preserved; four manual outcome rows were added intentionally.
Health remained HTTP 200 and the scheduler/legacy digests remained disabled.

## Discord Stage B: isolated scheduled reports (not activated)

Migration `core.0080_discord_report_schedules` adds:

- `DiscordReportSchedule`: one optional preference/cursor row per existing
  configuration and destination. Frequency defaults to disabled; empty reports
  default off. Planned immediate categories are preferences only, with no active
  enable flag or event hooks.
- `DiscordReportDelivery`: persistent unique schedule/slot claims and sanitized
  outcomes, independent of manual and legacy hourly history.
- `DiscordSchedulerHeartbeat`: a recent-check indication, not an inferred promise
  from a configured frequency.
- `ChallengeMonth.discord_community_opt_in=False` for existing and new Challenges,
  exposed through the already-authorized General Settings editor. It neither
  changes visibility nor publishes historical announcements.

Existing ciphertext, manual history and hourly digest fields/receipts are preserved.
An Owner explicitly switching an already-enabled legacy hourly digest to a new
Moderator schedule must select its retirement checkbox. This changes only the old
`enabled` flag, retaining its ciphertext and history; otherwise the new schedule is
rejected, preventing duplicate legacy/new reports. There is no automatic migration
or backfill of schedules. Source/schema rollback can leave the additive schema
in place; restore the pre-Stage-B source while keeping the new worker unactivated.
The private configuration/source/consistent SQLite backup is
`/srv/docker/northbound-dev/rollback-discord-stageb-1fd6500`.

Supported frequencies are Disabled, 30 minutes, hourly, 6 hours, 12 hours, daily,
weekly and Custom Times. Intervals align to Unix UTC boundaries, independent of the
server/Group clock. Fixed schedules use the existing IANA Group timezone. Daily
requires one time, Weekly one weekday and time; Custom Times applies its sorted,
unique HH:MM list to all selected weekdays. Duplicate or malformed times, invalid
weekdays/categories and invalid IANA timezones are rejected server-side. Group
Settings remains the timezone editor, affecting both destinations and existing
Group displays. Changing it resets future scheduling instead of replaying old slots.

Spring-forward nonexistent times are skipped; fall-back repeated times use the
first occurrence only. Local date/time slot keys prevent repeated-hour duplicates.
A dedicated processor checks every 30 seconds by default (15–60 seconds configurable),
so normal precision is roughly 30 seconds plus current processing/transport time.
Slots more than 90 seconds late are skipped, with one missed receipt rather than a
catch-up flood. Restarts/configuration changes schedule future slots. Claims commit
before network I/O, and are never automatically retried, including uncertain crashes
or partial multi-message delivery. Rate limits retain cooldowns; manual and scheduled
paths respect the same destination's cooldown without sharing delivery claims.
Webhook replacement/configuration changes during preparation stop the old attempt;
future slots use current settings. An in-flight HTTP request cannot be recalled.

Moderator reports filter the six existing review categories before calculating
Team/Challenge/Group task totals. Unselected/empty sections disappear; task eligibility,
Team attribution and scoring remain unchanged. Empty checks send nothing by default;
when explicitly enabled they send the existing All Caught Up card. Manual Current
Status remains independent and shows the complete current dashboard.

Community Challenge, registration, date, participation and results categories remain
unavailable pending an approved public-sharing policy. They are visible with an
explanation and are rejected if forged in a request. Challenge opt-in is necessary,
but never sufficient to bypass that policy. Community schedules with empty reports
off send nothing; explicitly enabled empty reports can send only the existing neutral
public card. No restricted Challenge data is queried or published. Immediate category
preferences are labeled Coming in Stage C; event detection, outbox/retries and event
delivery remain unimplemented. Unvalidated participation-milestone events are unavailable.

### Dedicated entry point and future activation

`run_discord_scheduler` imports no lifecycle processor. It neither transitions
Challenges nor changes submissions, scores or unrelated jobs. This command is
implemented but **no recurring service is enabled**:

```sh
# Read-only inspection is safe with all scheduling gates closed.
nbdev 'docker exec northbound-dev python manage.py run_discord_scheduler --once --dry-run'
```

Dry run creates no heartbeat, receipt or delivery. Normal execution refuses to run
unless `NORTHBOUND_DISCORD_SCHEDULER_ENABLED=1`, whose default is false. The settings
page distinguishes configuration readiness, recent worker heartbeat (90-second
freshness), accepted delivery and unknown/failed/skipped outcomes. One-time checks
are identified separately from recent daemon activity. Manual actions never move
scheduled cursors or mark reports delivered.

**Activation requires separate authorization.** The concrete steps would be:

1. Add `NORTHBOUND_DISCORD_SCHEDULER_ENABLED: "1"` only to the Dev Compose environment
   and recreate only Dev so both the app and worker see it. Preserve the two already
   approved manual Discord gates at 1 and `NORTHBOUND_CHALLENGE_SCHEDULER_ENABLED=0`.
2. An Owner configures each intended destination, category set, cadence and optional
   empty-report preference; leaves legacy hourly digests disabled; and confirms the
   Group timezone. Community content remains restricted as described above.
3. Start/supervise only `docker exec northbound-dev python manage.py
   run_discord_scheduler --interval 30` as a dedicated foreground worker, for example
   through a separately reviewed service unit. No such service/unit has been created
   or started by this task. Do not enable `run_challenge_scheduler` for Discord.
4. Verify heartbeat, next slots, privacy and actual accepted delivery after that
   authorization. No repeated real messages are authorized or sent in Stage B.

To stop future scheduled delivery, disable the dedicated worker/gate or a destination's
schedule; preserve history. Ordinary source synchronization must not replace approved
Dev environment values with repository defaults. Production remains outside this scope.

Stage B validation covered 112 focused Django cases across scheduling and regression
runs, plus the JavaScript visibility checks. Read-only Owner/Moderator renders for
all eight frequencies and validation errors passed at 375/440/1280px (54 page
combinations, each containing both destinations). Native frequency changes showed
only applicable time/day controls; Moderator fields stayed disabled. The migration
rehearsal and Dev cutover preserved all pre-existing row values across 63 tables,
webhook ciphertext, manual/legacy histories and 40 media files. This task initiated no real Discord
sends. Concurrent manual activity was preserved separately. Schedules, report
receipts and heartbeat remain empty, every
Challenge opt-in remains false, both recurring scheduler gates are off, and health
is HTTP 200. Moderator empty-state wording explicitly scopes itself to selected
report categories; manual dashboards continue to describe the full queue.

## Discord Stage B.1: compact settings and review embeds

The same Group settings route now provides Community and Moderator tabs with one
visible panel. Server selection and save/error redirects use a validated destination
query parameter. Native button tabs use tablist/tab/tabpanel semantics, roving focus,
Left/Right and Home/End navigation. Switching only hides the other panel; its forms
and unsaved values remain in memory. Changes are never stored in browser storage
or URLs. Leaving/reloading with unsaved edits uses the browser's standard warning;
saving one form still warns if another has unsaved edits. Without JavaScript, links
switch the same endpoint and explicitly advise saving first.

Connection summaries and manual buttons remain visible. Webhook management,
technical diagnostics, legacy digest information and recent sanitized histories are
collapsed under Advanced Settings. Invalid webhook forms open that section and
retain the correct tab. Scheduling still reveals only applicable time/day fields.
The six Moderator categories remain selectable; unavailable Community categories
are summarized in text rather than six disabled controls. Stage C is one Coming
Soon notice. Its existing saved preferences remain persisted: omission from this
compact form preserves them, while explicit values still receive choice validation.
No scheduling calculation, claim, gate, permission, encryption or model is changed.

Moderator review embeds put Group identity in the title, Challenge identity and
one prominent review-task count in the description, and compact nonzero category
lines in Team fields. All six categories and unassigned handling use the unchanged
count service. Each Challenge total appears on its first page only; later pages
say continued. Selected scheduled categories remain explicitly scoped. Existing
Unicode-safe budgets, safe links, timestamps, footers and pagination are retained.

Routing inspection found the two user-configured Dev destinations use the same
webhook endpoint. This can put Community and Moderator messages in the same Discord
channel without mixing their builders. Shared-endpoint regression tests verify
Community Test/Status and scheduled Community payloads never contain review data,
while Moderator actions retain private Team-grouped summaries. No webhook is copied
or modified to separate them; channel selection remains the owner's configuration.

The pre-deployment private backup is
`/srv/docker/northbound-dev/rollback-discord-b1-5615238`. This is a source-only change;
no migration or image rebuild is required. Recurring Discord and Challenge scheduler
gates remain off. Only the two explicitly authorized Current Status actions may be
sent during live validation; Stage C and recurring activation remain deferred.

Stage B.1 validation: 87 focused Django tests and both JavaScript checks passed;
Django checks and migration detection reported no changes. Read-only live-source
renders covered Owner/Moderator access, all frequencies and validation feedback.
Desktop browser checks confirmed tab isolation, keyboard switching, retained edits,
progressive Custom Times controls and disabled Moderator fields. The browser ignored
requested 375/440px viewport overrides (DOM viewport stayed 1280px), so mobile visual
verification remains outstanding; requested resize calls are not counted as passes.
Exactly one real Community Current Status and one Moderator Current Status were
accepted by Discord with HTTP 204, one embed each. Community payload validation
forbade private queries and matched the public-safe builder. All pre-existing table,
media and migration fingerprints matched after these two new manual history rows;
webhook ciphertext/settings and scheduled history were unchanged. Development health
returned HTTP 200. Manual testing remains enabled; both recurring scheduler gates
remain disabled. No production, image, workflow or Stage C change was made.

## Discord Stage C: event outbox (automatic delivery remains off)

Stage C supersedes the earlier policy-deferred Community category descriptions.
The approved opt-in policy permits non-Draft Challenge identity, dates and
registration metadata for the configured Community audience. This is an explicit
metadata exception; existing membership requirements on links remain. Participation
and results require authorization for the Discord audience, not merely member
visibility. Current rules do not authorize unauthenticated recipients, even after
completion or with Everybody standings, so participation/results are omitted.
Drafts are always excluded; opt-in completion lifecycle notices remain permitted. See
[DISCORD_EVENTS.md](DISCORD_EVENTS.md) for authoritative producer boundaries,
retry/uncertainty semantics, privacy rules and the exact controlled-test procedure.

The additive migration `core.0081_discord_event_outbox` adds default-off immediate
activation fields and a minimal outbox. Saved categories are retained without
activation or historical backfill. Existing scheduled slot calculations, webhook
encryption, manual claims and lifecycle scheduling remain unchanged. The companion
`run_discord_events` command requires `NORTHBOUND_DISCORD_EVENTS_ENABLED=1` plus all
existing transport/destination gates and explicit local immediate activation.
`--dry-run` creates no claims, heartbeat or messages. No daemon was started.

A consistent private Dev database/source/config backup was made at
`/srv/docker/northbound-dev/rollback-discord-stagec-d4d6ee5`. Migration was rehearsed
on a disposable copy before applying it only to Dev. Existing row values across
66 tables and all 40 media hashes matched after deployment; only the expected
migration was added. No event backfill or automatic activation occurred. The
existing image and environment files were retained; health returned HTTP 200.

Validation included a 220-test regression run, updated Community/manual/settings
coverage (89 focused cases), 36 final event/worker tests, JavaScript tab/scheduling checks and Django
schema checks. Responsive browser verification used sandboxed, fixed-width frames
of deployed read-only Owner/Moderator renders, because browser-level viewport
resize requests were unreliable. Frame widths 375/440/1280px were measured in the
browser; both destinations had no horizontal overflow. Custom controls, validation
feedback, keyboard tab navigation, retained edits and Moderator read-only controls
passed. No live form was submitted and no real Discord message was sent.

At completion the event and recurring Discord gates are off, the Challenge
scheduler remains off, immediate destination activation remains off, and manual
Dev testing remains enabled. Production, GitHub Actions, main, tags and releases
are outside this deployment.
