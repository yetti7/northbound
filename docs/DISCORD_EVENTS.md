# Discord Stage C operations and event boundaries

Stage C is implemented with **automatic delivery disabled by default**. Neither
saved Stage A/B categories nor the migration enable it. No worker is started by
web requests, application startup, migration, or the scheduled report worker.

## Authoritative event producers

- The successful `submission_create` transaction records the new book and its
  initial Theme/prompt decisions after acceptance, scoring and matching succeed.
- `review_submission_with_matches` observes book and Theme state transitions;
  `correct_submission` observes later review-required changes without changing
  correction semantics. Nested observer calls share one before/after snapshot.
- `replace_selections` and `review_prompts` observe Bingo/Scavenger book claims.
- `request_immersive` and `review_immersive` observe Immersive requests and decisions.
  A pending request becomes review-required when the existing queue considers it
  actionable (for example, after approval of its underlying book).
- `create_photo_submission`, `transition_photo_reservation`, and
  `review_photo_submission` observe standalone Photos. Nested reservation/review
  calls create one notification for the actual transition.
- `ChallengeMonth.transition_to`, `apply_scheduled_actions`,
  `set_manual_registration`, and `_save_challenge_schedule` observe lifecycle and
  registration changes under the existing Challenge lock. Registration is separate
  from lifecycle. Active -> Finalizing means submissions closed; Finalizing ->
  Completed means completed and final results released by existing permissions.
  Backward transitions are not announcements. No automatic event scans old records.
  Scheduled actions already overdue by more than 90 seconds are not announced.

These are explicit service observers, not page-render hooks or generic save
signals. Raw SQL, QuerySet.update, admin/model-only edits and recovery operations
are not alternate event producers. Participation milestones have no approved
thresholds and remain unsupported. Re-entry caused solely by an enrollment change
is not an observed submission-review transition.

## Outbox and reliability

`core.0081_discord_event_outbox` adds two default-off activation fields to the
existing destination schedule and one `DiscordEventDelivery` outbox table. It does
not backfill events. References include Group, Challenge, source kind/id, category,
optional Team, stable event UUID, activation timestamp, ciphertext hash, timing and
sanitized outcome. No URL, decrypted token, payload, username, book title, score,
photo URL or moderation reason is stored. Initial source events use deterministic
UUIDs; subsequent actual transitions receive a UUID once within their transaction.
The unique event/destination constraint prevents duplicate receipts.

Outbox rows commit or roll back with the business transaction. The worker commits a
conditional state/claim UUID update before preparing or posting a webhook. It
rechecks gates, activation, categories, destination enablement, ciphertext identity,
source eligibility and Community visibility. Changed/disabled/ineligible events are
skipped. Source events prefer review-required over new-submission when both selected;
this avoids two messages for one creation. Independent review tasks remain separate.
Team attribution uses the existing Moderator dashboard attribution rules.

Each destination processes older pending/claimed events first; one destination's
failure does not block the other. Manual/scheduled rate-limit cooldowns are honored;
event rate limits set the shared destination cooldown. Four attempts maximum.
Explicit HTTP 429 and 5xx use exponential backoff (30/60/120 seconds) with a larger
Discord Retry-After honored, bounded to 24 hours. Events expire after one hour.
First attempts missed by more than 90 seconds are skipped rather than replayed.
Timeout/network errors and claims interrupted for five minutes become uncertain
and are never automatically retried. Check Discord before any operator action.
5xx retries can duplicate a message if Discord processed a request before returning
an error: this is bounded at-least-once retry behavior, **not exactly-once delivery**.
No per-message idempotency support is assumed from Discord.

## Community sharing boundary

The supplied policy explicitly authorizes opted-in Challenge identity and schedule
metadata for the configured Discord audience. Existing `can_view_challenge` rules
make non-Draft Challenges visible to every active Group member; Drafts remain
private. Only opted-in, non-Draft Challenges in the same active Group are eligible.
This sharing opt-in does not create anonymous web visibility: direct links still
require the existing authenticated Group membership. Active/Upcoming identity,
dates and registration use this approved metadata exception. It does not authorize
participation, individual activity, standings or results for arbitrary Discord
recipients. Those fields use existing permissions with an unauthenticated audience
because no recipient identity/membership can be verified. Current Northbound rules
deny that audience, including Completed/Archived and “Everybody” standings; those
categories are therefore omitted. Saved selections are retained without granting
access. Results events are neither enqueued nor delivered while unauthorized.
Completion lifecycle announcements remain permitted basic metadata. No live scores,
Reader names or moderation data are included. Private/LAN application URLs are
omitted, including in development. No public route or visibility rule is added.
All six scheduled Community categories and manual Current Status use the same
projection; omitted categories cannot leak through other fields.

## Controlled live-test procedure (requires separate authorization)

1. Back up Dev and choose an existing destination/channel approved for its content.
   Keep the Challenge scheduler and scheduled Discord gate off.
2. In Group Discord settings, explicitly enable immediate notifications for one
   destination and select only the event category to test. Existing saved category
   selections alone do not activate capture. Preserve webhooks; do not print them.
3. Authorize a named fresh event and a one-time worker run. Set the **development**
   environment `NORTHBOUND_DISCORD_EVENTS_ENABLED=1` only for that approved test.
   Global manual delivery/development gates must also be enabled. Do not start a
   daemon or enable `NORTHBOUND_DISCORD_SCHEDULER_ENABLED`.
4. Preview with `python manage.py run_discord_events --dry-run` (read-only).
   Perform the single fresh normal application action, then within 90 seconds run
   `python manage.py run_discord_events --once`. Normal authorization/CSRF/scoring
   checks remain in force. Inspect sanitized history and the approved Discord
   channel. A one-time worker check does not establish daemon health.
5. Turn the Stage C global gate and local immediate activation back off after the
   test unless separately authorized to leave them on. Preserve receipts; do not
   clear uncertain claims or manually requeue them without checking the channel.

Continuous activation requires explicit approval for a dedicated supervisor using
`python manage.py run_discord_events --interval 30`. It must not run the Challenge
lifecycle scheduler. Recurring reports use their separate existing command/gate.
Production configuration and migrations are outside this implementation's scope.
