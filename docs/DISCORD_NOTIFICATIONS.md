# Group Discord review digests

Group Owners and Platform Administrators can open **Group → Discord Notifications**
to enable the digest, replace the webhook, or remove it. Group Moderators have
read-only access. Challenge staffing alone grants no access. Existing role
combinations retain their independent Group permissions.

Webhook URLs are credentials. Only exact HTTPS Discord/legacy Discord webhook
endpoints are accepted. URLs are stored with Northbound's existing credential
encryption and never redisplayed. Preserve `NORTHBOUND_TOKEN_ENCRYPTION_KEY`
(or the existing SECRET_KEY fallback) when moving/restoring a database.

## Deployment prerequisites

- Apply additive migration `core.0072_group_discord_notifications` during a separately
  authorized deployment. No existing Group is enabled and no historical records are
  changed by this migration.
- Configure `NORTHBOUND_URL` with the public Northbound origin for review links.
- Set `NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=1` explicitly. Default: off.
- Keep the existing Challenge scheduler running; no new worker is required.
- DEBUG or the development banner blocks delivery unless
  `NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=1` is explicitly set as a second opt-in.
  Do not enable either delivery flag in Dev without authorization.

Saving settings never sends a test message. The scheduler considers enabled,
active Groups on its existing polling loop. An empty queue sends nothing.

## What is counted

The reusable `group_pending_review_counts(group)` service counts **review tasks**,
not unique books, Readers, or points:

- Pending nonvoid book submissions in Active/Finalizing Challenges.
- Pending Theme decisions in the same ordinary review queue, including outstanding
  Theme decisions on rejected books. Each Theme claim is counted once.
- Pending Immersive requests from the existing shared queue candidate filter:
  approved eligible books only, in Active/Finalizing/Completed Challenges.

A book and its pending Theme decisions are separate actionable tasks, as in
Northbound's existing attention totals. Pending Immersive requests on unapproved
books are not double-counted as actionable reviews. Archived Challenges are excluded.
The digest summarizes Group-wide tasks; opening a linked review page still enforces
that reviewer's normal permissions. It sends counts, Challenge names, and links only.
No registration answers, Reader identities, book notes, or award details are sent.
Discord mentions are disabled in every payload. Very large digests retain the
Group-wide total and link to the Group for additional Challenges.

## Delivery guarantees and limitations

A database uniqueness constraint reserves each Group's UTC hourly slot **before**
HTTP delivery. A rolling one-hour cooldown additionally prevents back-to-back sends
across an hour boundary. Replacement/removal never clears the cooldown. Network I/O
runs outside database transactions, with TLS verification, a ten-second HTTP timeout,
and redirects disabled.

Failures do not abort other Groups or Challenge schedule processing. Discord 429
responses defer the next attempt for at least an hour, honoring a longer retry delay
(up to 24 hours). No automatic retry occurs in a reserved hour, including ambiguous
network timeouts. Outstanding tasks are recalculated in subsequent hourly attempts.

This is at-most-once **attempt** handling, not guaranteed message delivery: a process
crash or uncertain timeout can lose that hour's digest. A `claimed` status records an
unknown outcome and is not retried. Failure text is deliberately generic and does
not preserve provider response bodies or credential URLs. Settings show the last
attempt, last success, safe status, and next eligible attempt. An already in-flight
request may finish after settings are disabled or the URL is replaced.

Each request is bounded, but Groups are processed sequentially in the existing
scheduler. Very large installations may eventually need a separate delivery worker;
none is introduced here. Hourly delivery rows are retained; no automatic cleanup or
backfill is performed.
