# Northbound v1.1.2

A focused update to Discord consent, submission review, email administration and local-account recovery. Development acceptance is complete. These notes describe the approved release contents, not a claim that production deployment or publication has occurred.

## New and improved functionality

- **Discord consent and isolation:** Community and Moderator notification opt-ins are independent per Challenge. Current consent is enforced for Challenge content in queued events, manual reports and scheduled delivery. Different Challenges do not share messages, and skipped events are not revived by later opt-in. Existing webhook configuration and Community consent are preserved. Obsolete legacy digest information is removed from the settings interface without deleting delivery history.
- **Pending Theme decisions:** Authorized reviewers can approve or reject an eligible Pending Theme claim when the parent book is already Approved. Book identity, approved pages, status and existing approval evidence remain protected. Theme-specific scoring is applied once and decisions receive appropriate audit records; finalized history still requires the existing correction workflow.
- **Unified review cards:** Pending Immersive Reading requests appear in the existing responsive review-card grid alongside book and Theme reviews. Existing permissions, scoring and Approve/Reject/Details actions are retained.
- **Email Settings:** Platform Administrators manage provider-independent SMTP host, port, encryption, credentials, sender and Reply-To settings. Passwords are encrypted with Northbound's existing key mechanism, never redisplayed, preserved on blank edits and deliberately replaceable. Test-email feedback distinguishes SMTP acceptance from inbox delivery and invalidates stale test status after configuration changes.
- **Local password recovery:** Branded login/recovery pages use Django's established tokens and password policies. Links expire after 12 hours, responses avoid disclosing account existence, requests are rate-limited, and links use the configured trusted HTTPS origin. Normal login remains independent of SMTP availability.
- **Administration navigation:** Needs Attention appears immediately below Platform Administration in the administrator dropdown, using the existing count. The redundant Platform Owners shortcut is removed from that dropdown; its System Management card, permissions and management page remain available.
- **Development startup maintenance:** The live-source override applies pending restores, migrations and the restored-Hardcover safeguard before starting the existing two-worker, reload-enabled Gunicorn command. This repair is isolated in a maintenance commit and does not alter production Compose.

## Database changes

Apply the existing migration chain in order: **0082 → 0083 → 0084**.

- `0083_challenge_discord_moderator_opt_in` adds the per-Challenge Moderator Discord opt-in, **default disabled**, including existing Challenges. Existing Community opt-in values are not changed. Administrators must explicitly opt Challenges into Moderator delivery as appropriate.
- `0084_platform_email_settings` creates installation email configuration and recovery-rate-limit tables. Email and password recovery default **disabled**. No Development SMTP configuration or credentials are installed by this migration.

No historical scoring reconciliation or unrelated data backfill is part of this release.

## Upgrade considerations

1. Before a separately authorized production deployment, take a fresh, verified consistent database backup with matching media and configuration. Preserve the previous immutable v1.1.1 image and a protected off-host recovery copy. Do not substitute the earlier Development restore snapshot for a fresh Production backup.
2. Preserve the existing encryption material: `NORTHBOUND_TOKEN_ENCRYPTION_KEY`, or the original `DJANGO_SECRET_KEY` where the existing fallback is used. Backups contain encrypted credentials but not these keys.
3. No new environment variables, dependencies, workers or services are required. Retain the existing production `NORTHBOUND_URL`, HTTPS configuration, mounts, established worker commands and integration gates.
4. Quiesce affected writes and workers for the image/schema cutover. Apply migrations before the new web/worker code uses the schema, preserving the ordered startup and stopping on failure. Confirm no unintended pending restore exists.
5. Verify health, migrations, data/media preservation, consistent image versions and existing worker readiness before resuming normal operation. Preserve Discord destinations and Community consent; new Moderator consent remains off until explicitly selected.
6. Leave production email and password recovery disabled until the administrator configures SMTP separately through Platform Administration. **Do not copy Development Gmail credentials.** See [Email configuration](EMAIL_CONFIGURATION.md) for supported options, key recovery, rate limits and enable controls.

### Accepted infrastructure decisions

Production's existing HTTPS routing and internal HTTP forwarding are compatible with recovery and will be retained. Existing NPM configuration and logging remain unchanged; no Force SSL change was requested and no additional proxy infrastructure is required.

The administrator explicitly accepted the local reset-token logging risk identified in the production review: NPM logs can contain reset-token paths, query strings and referrers. This exposure is **not technically resolved**. Application-level reset-path redaction does not sanitize NPM logs. No further proxy work is a prerequisite of the approved release.

## Rollback considerations

Preserve the matching v1.1.1 image and pre-upgrade database, media, configuration and encryption material. Do not assume image-only rollback is sufficient after schema changes or compatible with all older write paths.

Reversing migration 0084 removes new email configuration/rate-limit data; reversing 0083 removes Moderator consent. Migration reversal therefore does not preserve those new settings. If rollback is required, stop affected writers/workers, retain failed-state evidence and follow an explicitly approved compatible restore procedure. Do not silently overwrite post-upgrade data or run an improvised reversal.

## Validation and acceptance

- **141/141** targeted local tests passed.
- **67/67** deployed-code tests passed using isolated synthetic data and blocked outbound connections; this is an overlapping subset, not 67 additional distinct tests.
- Migration consistency and whitespace checks passed. The application runtime remained unchanged after those tests.
- Development deployment applied 0083 and 0084 while preserving 818 submissions, 3 Challenges and all 44 media file hashes.
- The administrator completed real Gmail email/password-recovery acceptance using Jack's existing Development account: email received, trusted HTTPS link opened, new password saved and login succeeded.
- The temporary relay's `no-referrer` policy caused a browser null-Origin CSRF rejection. A relay-only correction to `strict-origin` resolved it without weakening Django security checks or changing application-wide behavior.
- Cleanup removed the temporary HTTPS listener/relay and restored Development's original HTTP configuration. Gmail SMTP and Jack's new Development password were preserved; Development password-reset delivery was disabled again. Automatic Dev Discord delivery remains disabled.
- Production was not modified during Development acceptance or cleanup. See the [acceptance record](V1_1_2_DEV_ACCEPTANCE.md).

Unchanged tests were not rerun solely to create the approved local commits. Publication, PR activity and production deployment require separate approval.
