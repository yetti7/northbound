# v1.1.2a Development validation

This records the initial uncommitted Development validation and focused release
preparation. Publication is a separate step using the reviewed release commit;
neither validation nor publication authorizes Production deployment. Existing
excluded repository files remain outside this correction. Historical iCloud
backups were not accessed.

## Focused validation

- 12 process-supervision tests passed, including actual fake-child subprocesses
  for worker failure, unexpected exit zero, Gunicorn failure, SIGTERM/SIGINT
  forwarding, bounded forced shutdown, reaping and duplicate-start prevention.
- The same 12 tests also passed through Django's maintained `core` discovery
  after placement in `core/test_process_supervision.py`.
- 9 focused Django tests passed: `DevelopmentRuntimeTests` and the existing
  Hardcover startup/health contract test. No full-suite or CI runs were requested.
- Final combined discovery/run of all 21 focused tests passed after the startup
  fixture changes; Django reported no system-check issues. `git diff --check`
  also passed.
- Release preparation added a controlled integration check of both real Discord
  management-command loops under one supervisor, alongside a fake web process.
  Both workers started exactly once, completed safe iterations, remained alive,
  and were reaped after supervisor shutdown. A dedicated SQLite fixture contains
  only the heartbeat table: no user notification preferences or live database.
  Global delivery is disabled; socket connections and webhook transport are also
  blocked, with assertions that no delivery was attempted. Both original
  30-second intervals are retained. The final focused suite passed all 22 tests.
- Existing commands and 30-second intervals remain unchanged. PostgreSQL's
  intentionally exiting SQLite-only backup command is omitted there.

## Development deployment

Local-only image:

`northbound:v1.1.2a-dev-20261116t070737z`

Image ID:

`sha256:905bb1f21bc4cf40e72830e808cd3d0a3ffb948832b311215924a725d5ea341f`

The image layers only the two startup files onto the already-cached approved
v1.1.2 base. It labels the revision `uncommitted-development`, with the supervisor
file hash, rather than claiming a published source revision. Application settings
and OCI version label report the exact `v1.1.2a` identifier.

Only the existing Development image override and live-source startup override
changed on deepnorth. The four-file Compose list is unchanged. Effective config
comparison allowed only image, command, init, stop grace and Gunicorn reload
arguments. Source/data mounts, ports, other environment values and base configs
were identical. Gunicorn retains polling reload through `GUNICORN_CMD_ARGS`.

Verified after recreation and one controlled stop/start:

- `/health/`: HTTP 200.
- One supervisor, one Gunicorn master, two Gunicorn workers; restart count zero.
- Graceful Development stop: exit code zero; Gunicorn shutdown messages present.
- No automatic management workers, matching existing Development gates.
- Existing global/manual Discord allowance remains on; event and report gates
  remain off. Nothing was enabled for testing and no notifications were sent.
- All 71 database-table fingerprints unchanged; 818 submissions retained.
- All 44 media files retained with identical SHA-256 hashes.
- SMTP/account/consent data are covered by the unchanged database fingerprints.
- Production's three container IDs/start times and Compose SHA-256 unchanged.

Protected on-host rollback/config/data evidence and the unapplied Production
proposal are under:

`/srv/docker/northbound-dev/recovery/v112a-supervisor-20261116T070737Z/`

`validation-result.json` contains the sanitized acceptance result. Private config,
database snapshot and logs remain on-host in that mode-0700 directory, outside
the minimal image build context. They are retained for rollback/review.

## Version and publication boundary

`git check-ref-format refs/tags/v1.1.2a` succeeds. Workflow `push.tags: ["v*"]`
matches; image metadata uses literal tag references, not SemVer parsing. The
Docker tag format and application version display accept the identifier; the
Development image verifies it in practice. No dependency/version parser change
is necessary. The existing stable-image `latest` policy excludes this suffix;
the Production candidate therefore selects the explicit versioned image. Do not
assume a `latest` tag promotion or published multi-architecture release exists.

Before approval, review the production proposal and transition/rollback plan in
`V1_1_2A_DEPLOYMENT_PLAN.md`. Existing uncertainty/missed-window rules for Discord
remain unchanged. One failed process now restarts the whole application service;
an unresponsive but live worker still requires existing heartbeat diagnostics.
