# Northbound v1.1.1

Release candidate notes; publication and production deployment are pending.

## Submission Management

- A unified staff workspace brings Overview, Edit Book, Qualifications, Review & Scoring and History together while preserving scoped permissions and separate confirmations.
- Readers can edit their own eligible Pending submissions and request post-submission BOTM/TBR qualification review. Requests do not grant credit automatically.
- Book corrections use signed, stale-safe impact previews and atomic audit history. BOTM/TBR selection distinguishes Reader intent, strong identity evidence and uncertain suggestions.
- Hosts, Floaters and Team Leaders can manage exceptional BOTM/TBR credit within their existing review scope. Participating Hosts remain Team-scoped; Team Leaders remain assigned-Team scoped. Group Owners retain existing authority, and Platform Administrators retain platform scope. No new self-approval or Challenge configuration authority is granted.
- Pending submissions can be Approved or Rejected. Approved submissions receive authorized corrections through the existing correction workflows; ordinary Review cannot change their status, pages or Theme decisions. Invalid Approved submissions can be Voided through the separate authorized workflow, removing applicable current credit while retaining history. Approved-to-Pending reversal is not required or provided. Immersive review remains independent; protected frozen awards retain their existing restrictions.
- Correction-owned transactions now work with Discord observation without weakening transaction or privacy safeguards.

## Discord

- Separate encrypted Community and Moderator destinations support manual Test Message and Current Status delivery, rich embeds and Team-grouped Moderator information.
- Opt-in immediate lifecycle events and independently scheduled reports have Group preferences, per-Challenge controls, bounded retries, receipts, heartbeats and delivery diagnostics.
- Community announcements never announce Draft Challenges. Explicitly opted-in non-Draft Challenge metadata may reach the configured Discord audience without changing Northbound page access. Participation, standings, results and private moderation information retain their existing disclosure restrictions.
- **Automatic Discord delivery requires separate configuration, audience review and activation.** Installing v1.1.1 does not authorize destinations, workers, scheduled reports or legacy digests. All global delivery gates must remain off during the initial production release.

## Team Books

- A compact responsive gallery shows recorded page counts, safe book-reference links, improved cover placeholders and accessible full-title disclosure for truncated titles.
- Existing filtering, ordering and access rules remain intact.

## Branding and artwork

- Saved branding previews and advanced banner/gradient coverage and darkness controls support Groups, Challenges and Teams, with optional inherited-logo hiding for Teams.
- Legacy appearance remains the default until customization is enabled.
- Readers can upload or replace their own Personal TBR covers within existing ownership and lifecycle limits, including eligible locked-in TBR entries. Upload validation and normalization preserve provider artwork as a fallback.

## Upgrade requirements

Apply `core` migrations **0078–0082** after 0077: branding preferences, manual Discord destinations, report schedules, event outbox and Personal TBR custom covers. No historical reconciliation/backfill is part of this upgrade.

Before deployment, quiesce all writers and obtain a fresh verified consistent SQLite database and matching media recovery point. Preserve the exact production configuration and original Django/encryption keys separately. Do not replace keys, copy Dev data/configuration or assume an older image can use the upgraded database.

Retire the obsolete production `compose.tbr-title-2647958.json` command override during the separately approved deployment. Restore the normal ordered startup (`restore_pending_backup`, migrations, restored-Hardcover safeguard, then workers/web); stop on failure. Preserve one Challenge scheduler and the existing backup/Hardcover configuration. Keep all four Discord global gates false, legacy delivery disabled and both dedicated Discord workers absent.

See [the deployment and rollback plan](V1_1_1_DEPLOYMENT_PLAN.md) for exact sequencing, recovery requirements and pending release gates. These notes do not claim CI, image publication or deployment success.
