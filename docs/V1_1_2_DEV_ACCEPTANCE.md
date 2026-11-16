# v1.1.2 Development deployment and acceptance

Deployed 2026-11-15 to the existing `northbound-dev` on deepnorth. Production deployment and release work were excluded.

## Candidate and rollback

- Deployment source: branch `codex/v1.1.2-discord-opt-ins`, then-uncommitted working tree based on `7898877c713e00115439b4d2f659e26a69c8cb42`.
- Deployed 593 runtime files from `core`, `reading_challenge`, `templates`, and `static`, including new untracked application files and migrations. Tests, scripts, documentation and environment files were excluded.
- SHA-256 of the sorted runtime checksum manifest: `3c9fb02259deb363767c52687ee282f62c3a32276ac932a2036a3f83a1d903d0`.
- Protected remote rollback: `/srv/docker/northbound-dev/recovery/v112-deployment-20261115T233408Z` (0700). Includes a verified consistent SQLite snapshot, any companion files present, all media, original source archive, Compose files/effective configuration, source manifest and private validation output. Previous recovery history remains intact.
- Existing image unchanged: `northbound:dev-local-v110staticfix-20261114T034506Z`. Existing Dev web container was recreated; no additional persistent application containers were added.

## Deployment verification

Stopped only Dev during source cutover to avoid reload workers querying new fields before migration. Preserved the existing Compose file list, image, environment, networking, mounts, restart policy and two-worker polling-reload Gunicorn command. Preserved startup order: pending restore, migrations, Hardcover safeguard, Gunicorn. No pending restore existed or was applied.

Both `0083_challenge_discord_moderator_opt_in` and `0084_platform_email_settings` applied successfully. Final migration drift check reports no changes. No startup exceptions or restart loops were detected. Only Gunicorn master and two workers run in Dev.

All 7,092 pre-existing database rows matched their original columns immediately after migration. Submission count stayed 818; Challenge count stayed 3. All 44 media files retained their hashes. SQLite integrity passed. Existing Community opt-ins and encrypted Discord configuration were preserved; all three new Moderator opt-ins started disabled. Final Theme/provenance counts and Discord receipt/event counts also matched the backup.

Dev and production health both returned HTTP 200 before and after. Production container ID/start time and Compose checksums were unchanged. Existing production workers remained operational; no production configuration, records, migrations or restarts were performed by this operation.

## Functional acceptance

- Local focused suite: **141/141 passed** across email recovery, Discord consent/events/manual/scheduling, pending Theme review and unified review cards.
- Deployed-code subset: **67/67 passed** inside the existing Dev container, using an isolated synthetic test database and media directory, temporary test modules and blocked outbound socket connections. This is an overlapping subset, not 67 additional distinct tests. Temporary test data was removed.
- Deployed checks covered encrypted SMTP persistence, blank-password preservation/replacement, sanitized errors, status invalidation, permissions, CSRF, generic recovery responses, rate limits, 12-hour expiry, successful password changes, token invalidation and canonical-origin generation with mocked delivery.
- Discord checks covered destination-specific opt-ins, current consent on queued/scheduled/manual delivery, per-Challenge message separation and no revival of skipped events. No live webhook transport was used.
- Theme checks used synthetic records: approval/rejection of Pending Themes on Approved books, preservation of identity/pages/approval evidence, correct one-time scoring and audits. Imported Dev submission 801 and Theme claim 217 were inspected only: Approved, 384 approved pages, Pending 25% Theme. Browser inspection confirmed protected book fields and enabled pending-Theme controls. Imported records were not edited.
- Browser checks on actual Dev: Email Settings card/page, saving disabled defaults, blank password field, editable test recipient, administrator menu order, matching Needs Attention count (7), desktop button retained, owner management card retained, login recovery link and identical known/unknown generic confirmation.
- Review grid inspected at desktop and 390px mobile widths in light/dark styling: one grid, pending book/Theme cards plus one pending Immersive card, Approve/Reject/Details links, no duplicate Immersive section and no horizontal overflow. No imported review action was submitted.
- Disposable browser administrator was created for acceptance, signed out and removed. Its audit history was retained. Imported accounts/passwords were untouched.
- A deliberately invalid, non-bearer reset-path probe verified actual Gunicorn access-log redaction: probe text absent, `[redacted]` present.

## Initial email and proxy observations (before live acceptance)

At initial deployment, email and password recovery were disabled and SMTP was unconfigured. No personal Gmail configuration was written to repository source or defaults, and no real email was sent during that initial phase. Credential and transport acceptance used mocked delivery against synthetic configuration. The final live acceptance below supersedes this initial status.

Dev currently uses direct LAN origin `http://192.168.0.11:8061`. Read-only NPM inspection found no proxy host forwarding to port 8061 or `northbound-dev`. NPM's `proxy` and `standard` formats include both `$request_uri` (path/query) and `$http_referer`, so token-bearing requests/referrers could be recorded if recovery is routed through NPM. No existing Dev-specific mitigation was found because no matching Dev proxy host exists. No NPM configuration was changed.

The initial investigation identified the need for a trusted HTTPS Dev origin and review of upstream logging before live testing. HTTPS was later supplied by a temporary private Tailscale endpoint and a nonlogging loopback relay. The initial suggestion of no-referrer handling proved incompatible with browser CSRF submission and was corrected as recorded below. No NPM configuration was changed. These historical observations do not impose new proxy requirements on the accepted production deployment.

Manual Discord remains enabled. Automatic event/report delivery and the Challenge scheduler remain disabled; no separate automatic Dev workers run. Imported destinations were not rewritten. Existing event/manual/digest receipt counts stayed unchanged.

## Final live email and password-recovery acceptance

The administrator completed and accepted the real-world test on November 15, 2026:

- Gmail SMTP was configured manually through Email Settings; a real SMTP test email was successfully delivered.
- Jack's existing active, normal Development account was used. No new Northbound account was created for the live reset, and its registered email was not changed.
- The administrator received the reset email, opened the trusted private HTTPS link, saved a new password and successfully logged in as Jack using that password.
- The administrator performed the request and password change personally. Preparation did not generate a real reset token or request an email automatically. No passwords, hashes, reset tokens, SMTP secrets or private reset links are included in this report.
- The temporary relay initially returned `Referrer-Policy: no-referrer`. Actual browser observation showed `Origin: null` with both the CSRF cookie and form token present; Django rejected the origin.
- Changing only the temporary relay's response policy to `strict-origin` resolved the failure. A browser request using a verified nonexistent synthetic address reached the generic confirmation; an invalid-email request passed CSRF and reached form validation. A null Origin remained rejected. CSRF, HTTPS and application-wide security checks were not disabled or changed.

## Completed temporary HTTPS cleanup

- The test-only Tailscale Serve listener on port **8443** was removed. Serve configuration exactly matched its preserved pre-test state afterward; unrelated Tailscale services were preserved.
- The detached loopback relay was stopped, port **18061** was closed, and its temporary implementation/PID/diagnostic files were removed. Protected recovery evidence was retained.
- The exact original Development URL, `http://192.168.0.11:8061`, and original cookie/proxy configuration were restored. Only the existing Dev web container was recreated, retaining the repaired startup, two workers, reload behavior, mounts and networking.
- Gmail SMTP remained enabled with its saved configuration and encrypted credential unchanged. Jack remained active, and its post-test password hash was unchanged during cleanup; the pre-test password was not restored.
- Password-reset delivery was disabled after returning to HTTP. The 12-hour token timeout and trusted-HTTPS safeguards remain unchanged.
- Development retained 818 submissions, 3 Challenges, migrations through 0084 and all 44 media file hashes. Application source matched the accepted runtime manifest. Normal HTTP login passed CSRF and reached validation; authenticated Email Settings rendering returned 200.
- Dev and production health returned HTTP 200. Production was not modified or restarted during acceptance or cleanup.
- Automatic Dev Discord events, scheduled reports and Challenge scheduling remain disabled; manual Discord testing remains available. No Discord destinations were rewritten or live Discord messages sent.

## Accepted production infrastructure decisions

The separate read-only production review confirmed compatibility of the existing public HTTPS origin and proxy forwarding with Django's secure cookies and CSRF settings. The administrator elected to retain existing HTTPS routing, internal HTTP forwarding, NPM configuration and NPM logging, with no Force SSL change or new proxy infrastructure.

NPM logs can include sensitive reset-token request paths, query strings and referrers. The administrator explicitly accepted this local reset-token logging risk. The exposure was investigated, not technically corrected; application logging redaction does not change NPM's logs. This accepted decision is not a release blocker.

## Scope boundary

Development acceptance and cleanup are complete. Production remained on v1.1.1 and was not modified. No GitHub publication or production deployment was part of acceptance. The later approved local release commits organize the already accepted source and this documentation; they do not constitute deployment or publication.
