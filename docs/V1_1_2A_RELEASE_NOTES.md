# Northbound v1.1.2a

Corrective release: restore the single-container application architecture.

- The startup script retains restore, migration and Hardcover restore-safety
  initialization, then hands off to a Python standard-library supervisor.
- Gunicorn and enabled Backup, Challenge, Hardcover, Discord event and Discord
  report workers run once each in the same container. Their existing commands,
  feature gates and execution intervals are retained.
- Any unexpected required-process exit stops the remaining processes and fails
  the container so its restart policy can recover it. Shutdown forwards signals,
  uses a bounded grace period, terminates remaining process groups and reaps
  direct children. Docker's built-in init handles orphaned descendants.
- PostgreSQL does not start the SQLite-only backup daemon, whose existing
  command intentionally exits on PostgreSQL.

No changes to Discord consent, destination preferences, Challenge isolation,
delivery records, deduplication, webhooks or scheduling logic. No migrations or
new dependencies. Existing data and administrator configuration remain in place.

Operators must stop the old Discord worker containers before starting the
consolidated image. See `V1_1_2A_DEPLOYMENT_PLAN.md`; image updates alone must not
leave old workers running alongside the new supervisor.

Version `v1.1.2a` is a literal supported Git/GitHub/GHCR tag and application
display value. It is not a SemVer identifier. The current `v*` workflow and
`type=ref,event=tag` image metadata rule support it without renaming. The existing
numeric-only release-tag policy does not automatically promote this tag to
`latest`; this corrective release should be selected explicitly by its verified
versioned image/digest. Publishing this correction does not authorize the
Production transition. Preserve the existing `latest` image and promotion policy.
