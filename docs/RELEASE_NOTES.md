# Release notes

## Northbound v1.1.0

A cumulative upgrade from v1.0.9.1, including the production title-only Personal TBR Recovery hotfix.

- Separate registration/challenge scheduling, clearer lifecycle actions, registration-answer corrections, consistent contextual navigation, and mobile administration/submission improvements.
- In-place eligible Personal TBR title/author corrections; streamlined submission editing, history/activity, scoped global search, Team Books and numeric roster sorting.
- Preserved participating-Host, Floater and Team Leader boundaries, independent score/standings audiences, historical lifecycle locks, and audited Advanced Recovery safeguards.
- Historical participation, late approval, earning evidence and frozen award preservation, using authoritative Reader/team scoring.
- Group/Challenge/Team branding, privacy-first Chronicle/Wrapped recaps, slideshows and PNG exports with verified Reading Highlights covers.
- Reader Rumble with fixed 1080×1350 PNGs, Top 5–25 layouts, bonus/zero-score controls, scoped rosters and matching previews/downloads.
- Bingo/Scavenger Book and Photo workflows: independent review, frozen prompt/completion credit, corrections, void/restoration, progress and scoped reports.
- Optional encrypted Group Discord webhooks with Team-grouped hourly review digests, bounded multipart messages and credential safeguards.
- Clean Dev builds prevent image-layer accumulation, metadata contamination and stale collected static assets.

### Database upgrade required

From v1.0.9.1, apply **core migrations 0067–0077**, ending at `0077_scavenger_photo_foundation`. Existing records/scores/media are preserved; no historical participation backfill or retroactive prompt awards are performed. Django creates the expected new model permissions/content types.

Before production changes, obtain explicit deployment approval and take a fresh consistent SQLite backup (backup API, integrity and foreign-key checks), preserve media/configuration/encryption keys, and record current scores. Keep the previous immutable image and paired database/configuration rollback. Do not copy Dev configuration, fixtures, databases or its pending restore archive into production.

Pin the verified release image/digest, quiesce web writes, explicitly run migrations against the intended production database before starting the new application, then verify health, workers, migration state, record/media preservation and current Reader/team totals. Do not reconcile or backfill scoring as part of upgrade. Never use image-only rollback after schema changes; preserve failed state and restore the compatible database/image/configuration pair if needed.

### Activation and validation

Bingo/Scavenger readiness remains **disabled in code**, both categories default disabled, and November 2026 GuildWars remains excluded. Environment variables alone do not activate prompts. Discord delivery defaults disabled and requires separate Group configuration/authorization. Preserve the existing production scheduler/Hardcover settings and secrets; this release does not authorize integration activation.

Phase E: **PASS WITH CONDITIONS**. Production-backup rehearsal: **PASS**, migrations 0066→0077, 61 users, 717 submissions, all original rows and 35 media files preserved; Challenge 259,003 and Team 138,961/120,042 points unchanged. These are backup-snapshot values, not assumed live-production baselines.

Known limits: one documented test assertion compares a Group-local Photo earning date to UTC and can fail around the date boundary; no application scoring change is indicated. Covers without CORS support fall back to text. Partially delivered Discord digests do not resume within the same claimed hour. Live external integrations, touch swipe and PostgreSQL-specific row locks were not independently revalidated in final acceptance; the previously documented administrator table overflow remains noted.

This is a **source release**, not a production deployment. GHCR image availability depends on the normal GitHub Actions workflow completing successfully; verify the `v1.1.0` image digest/revision before any production update. Tagged builds set `NORTHBOUND_VERSION=v1.1.0`; Dev builds intentionally retain their development version.

## v1.0.9

### Submission management and challenge credit

- A compact Manage Submission panel separates book information, Link Only, Update Book Information, Pages/Themes, challenge credit and Advanced Corrections. Platform Administrators can inspect submission-specific history.
- Reader Submit Book now includes responsive BOTM and confirmed Personal TBR cards. Readers may select either or both, change or remove selections, and retain their intent while searching Hardcover or entering books manually.
- Matching distinguishes Reader intent, book identity and awarded credit. Strong shared Hardcover work identities qualify across editions and formats; uncertain subtitles or incomplete identities produce reviewable suggestions. Different volumes, adaptations and conflicting catalog identities remain protected.
- Floaters and Team Leaders can confirm supported BOTM/TBR suggestions during normal, scoped approval. Approving the book alone leaves uncertain credit unconfirmed. Signed review snapshots reject changed or repeated requests.
- Platform Administrators, Group Owners and scoped Hosts can preview exceptional BOTM/TBR assignments, reversals and restorations. Reasons are required, hard identity conflicts need acknowledgement, and decisions/audits survive automatic synchronization.
- Existing reward services remain authoritative. Duplicate credit, unrelated record changes and historical Hardcover write-back are prevented. Operations that change an existing frozen completion award are blocked for separate historical review.

### Challenge visibility

- Before Completed, challenge-card aggregate pages and book counts are visible only to Platform Administrators (`is_superuser`). Completed and Archived cards retain their existing formatting for authorized viewers.
- Completed/Archived competitive standings and Reader Scores are available to active Group members without requiring enrollment or team assignment. Earlier states retain configured audiences; challenge discovery and private review/administrative permissions remain separate.

### Upgrade and compatibility

- Apply additive migration `core.0065_reader_intent_and_matching_evidence`. It adds optional Reader selections and matching evidence with neutral defaults; it does not backfill selections, create matches, recalculate historical scores or queue provider writes.
- Keep the existing database, media, deployment environment and Django/encryption keys. No new production secrets or runtime dependencies are required.
- Optional development isolation flags retain production defaults: schedulers and Hardcover write-back remain enabled when their flags are unset; the development banner remains off. Do not copy the Dev environment, override or synthetic fixtures into production.
- Retain a verified database/media backup and the previous immutable image. Older matching code does not understand new administrative decision evidence; an image-only rollback is not a supported recovery plan.

### Validation and accepted limits

- Functional Dev acceptance used an isolated synthetic Group, real Reader/Floater/Host browser actions and deployed endpoint checks. All original 3,402 rows across 55 tables, including 274 community submissions, remained unchanged; no Hardcover outbox entries were created.
- Final review covered 251 targeted Django cases, including the corrected history-locked BOTM regression. One legacy POST fixture was updated to include the required review snapshot; its focused rerun passed. JavaScript interaction/syntax and migration checks passed.
- Live Hardcover testing in Dev could not authenticate because copied credentials do not decrypt under Dev's encryption key. Cached/simulated checks passed. This is an accepted limitation; perform a read-only live catalog smoke check in the authorized production environment after deployment. No key recovery, credential copying or OAuth changes are part of this release.
- Different-work translations still require explicit reviewed resolution. General fuzzy matching, supplemental historical associations, historical repairs, library features, Discord integrations and new competition features remain deferred.

## v1.0.8 — November 4, 2026

### All Submissions improvements

- Platform Owners can search Challenge submissions by Reader username, display name, or book title. Results update while typing without reloading the page.
- Filter by All, Pending, Approved, Rejected, or Voided, with server-side pages of 25, 50, or 100 submissions.
- A cleaner table keeps Reader identities, book titles, base/bonus/total pages, status, and authorized actions easy to scan.
- Floating action menus remain usable after results update. Pending badges stay on one line, and long Reader names retain accessible full names.
- The neutral All Submissions heading and a single visible result counter preserve existing access boundaries and accessible announcements.

No additional database migrations are required. Submission permissions, scoring, voiding, and permanent-deletion safeguards are unchanged. View as User and Act as User are deferred.

### New features

- Immersive Reading: optional, configurable percentage bonuses for reading and listening to a book, with separate staff approval.
- Registration Agreements: customizable Challenge terms with recorded acceptance and preserved wording history.
- Authorized submission editing and corrections, with updated scores and retained submission history.
- Individual Hardcover metadata refresh for Personal TBR and Book of the Month selections.

### Improvements

- Display metadata stays separate from original TBR/BOTM matching evidence.
- Themes show eligibility dates and update selections when completion dates change.
- Hardcover TBR search opens beside the relevant book; Enter searches without saving the form.
- The combined Competition Visibility option includes Hosts, Floaters, and Team Leaders while preserving Team Leader team scope.
- Sticky identifying columns keep Readers and books visible in wide tables.

### Upgrading

Back up before upgrading. Existing SQLite installations upgrade through migrations **0061–0064**. Immersive Reading and Registration Agreements are **disabled by default**. Existing registrations and historical scoring records are preserved.

## v1.0.7 — November 3, 2026

### Submission and Personal TBR hotfixes

- Invalid completion dates now show helpful form errors instead of an HTTP 500,
  with the Challenge's inclusive permitted date range displayed on the form.
- Hardcover-verified submissions now update matching Personal TBR completions
  automatically, without granting duplicate credit.
- Missing or unavailable book covers use consistent placeholders across TBR and
  BOTM cards, while preserving completion badges and mobile layouts.
- Authorized administrators can preview and explicitly confirm missing Personal
  TBR matches using a controlled command. Historical awards and checkpoint
  snapshots are preserved; reconciliation never runs automatically.
- Production error logging now provides privacy-conscious exception diagnostics.

No database migration is required. Deployment and historical reconciliation are
separate operations. Metadata refresh and submission editing are deferred to v1.0.8.

## v1.0.6 — October 31, 2026

### Registration confirmation hotfix

- Fixed an HTTP 500 when confirming registration with a blank optional numeric
  signup answer. Blank answers now save correctly with or without Challenge TBR.
- Zero and decimal answers retain their values; required numeric questions still
  require an answer.

No database migration is required. Existing registration requirements, TBR rules,
permissions and Hardcover synchronization are unchanged.

## v1.0.5 — October 31, 2026

### Challenge TBR access and registration fixes

- Platform Owners can now manage any Reader's Challenge TBR across all Groups
  and Challenges without Group membership or a Challenge staff assignment.
  **Manage TBR** is available on participant lists and profiles.
- Removing a book before confirming registration now removes it from the saved
  TBR. Remaining selections retain their correct order and positions, and reused
  slots start empty instead of restoring a removed book.
- Invalid registration edits cannot confirm an older saved summary. Correct the
  errors and review the updated registration before confirming.

Existing Reader locks, protected history, role scopes, nine-book completion
requirements and Hardcover credential isolation remain in place. No database
migration is required; Hardcover synchronization is unchanged.

## v1.0.4 — October 31, 2026

### Challenge TBR cover previews

- Readers and authorized staff can see cover thumbnails in Hardcover search
  results and TBR selection/editing rows, including previously saved covers.
- Manual books support an optional HTTP/HTTPS **Cover URL**. Existing entries
  without artwork still work, and missing or unavailable images use placeholders.
  No image upload is required.
- New Hardcover selections retain the chosen edition's artwork when saved,
  including when another edition supplies scoring pages. Existing saved cover
  snapshots are preserved.

No database migration is required. Challenge permissions, registration behavior,
scoring, historical awards and Hardcover synchronization are unchanged.

## v1.0.3 — October 31, 2026

### Staff-assisted Challenge TBR management

- Group Owners and Moderators can help participants in their Group plan their
  Challenge TBR. Hosts and Floaters can help within their assigned Challenge;
  Team Leaders can help only Readers currently on their own team.
- **Manage TBR** lets staff add, remove, replace and reorder selections, search
  Hardcover or enter books manually. **Save TBR** preserves an incomplete list;
  **Save & Complete TBR** finalizes nine valid selections without completing
  Challenge registration or bypassing its requirements.
- Staff can make protected corrections after the Reader's editing lock. Books
  with match or award history keep their selection and position; conflicting
  changes are blocked. Earned bonuses and historical results are preserved,
  and changes show who last updated the list.
- Readers do not need a Hardcover connection for staff assistance. Staff search
  uses their own connection first, then the Group catalog connection, with
  manual fallback. Reader credentials stay private. TBR changes never sync
  reading activity to staff or Group Hardcover accounts; the Reader's own
  optional, consented forward-only sync remains separate.

No database migration is required for this feature.

## v1.0.2 — Draft, unreleased

Prepared locally for review. No release, tag, image publication or deployment is
implied by this draft. Tagged builds continue to supply `NORTHBOUND_VERSION`.

### Theme percentage bonuses

- Newly created Themes use independently configurable whole percentages from
  0–100%, default 0%, calculated from **Approved Pages** only.
- Each Theme rounds half-up separately to whole pages. Stacked percentages use
  the same base and add their awards; they do not compound.
- The rate locks after the first claim. Calculation snapshots retain rate,
  approved-page basis, result and finalized state, including zero-page awards.
- Intentional Approved Pages corrections update established percentage awards,
  including rejected claims, without approving or activating those claims.
  Reapproval uses the corrected award; ordinary repeat review does not recalculate.
- Authorized restoration establishes a first-time award for approved claims whose
  books were ineligible when recovery approved them. Zero awards are finalized and
  canonical provenance is not duplicated.
- Existing Themes remain legacy fixed-page rules throughout their lifecycle.
  No fixed value is reinterpreted as a percentage and no conversion UI is added.

### Migration and data preservation

Migration `0060_theme_percentage_awards` depends on `0059`. It adds rule and
snapshot fields, identifies existing Themes as legacy, and changes the default
for future Themes to percentage. Existing awarded pages, provenance amounts,
submission score caches and historical checkpoint snapshots are not recalculated.
Ambiguous legacy zero awards retain their previous behavior rather than invented
historical calculation evidence. Historical migration files are unchanged.

### Validation and release gates

Maintained test sources are versioned, remain outside production image contexts,
and are required before image publication. SQLite/PostgreSQL migration checks,
Compose checks, architecture builds and existing version-tag behavior are retained.
Four stale/time-dependent test expectations are corrected without changing working
application behavior. See [automated validation](TESTING.md) for suite scope and
local-only development-demo tests. Final validation results belong to the release
review; this draft does not assert a published CI run or deployment acceptance.

### Deployment and rollback

Plan a maintenance window and verify a pre-upgrade backup before starting the new
image: startup applies migrations automatically. Preserve matching media, original
Django/encryption keys, deployment configuration and the previous image digest.
SQLite schema changes may rebuild tables and need free space; PostgreSQL requires
native backup procedures. Keep the current database backend and volume identity.

Pin the reviewed release image/digest. Main-branch pushes and stable version tags
can update `latest`; image publication is distinct from deployment authorization.
Do not run an old image against percentage-era data as an assumed rollback.
Reversing 0060 removes calculation metadata and does not convert new percentage
Themes into equivalent fixed rules. The safe recovery plan uses the pre-upgrade
backup, matching media and compatible image, with the documented restored-Hardcover
safeguards before workers resume. Restoring local data cannot undo external
Hardcover writes. See [backup/restore](BACKUP_RESTORE.md) and [upgrades](SELF_HOSTING.md#upgrades).

## v1.0.1 — September 2, 2026

Focused fresh-install polish following the released v1.0.0 baseline, manually
accepted on September 2, 2026. Runtime version reporting remains supplied by
`NORTHBOUND_VERSION`; tagged image builds already inject the exact release tag.
There is no separate hardcoded package version to bump.

| Item | Root cause and implementation |
| --- | --- |
| Bind-mount startup | A host directory owned by another account prevented the container from creating its maintenance lock. Self-hosting docs now explain UID/GID `10001:10001`, directory creation and ownership. Default named-volume deployment stays intact and needs no manual ownership handling. |
| Group token success | The asynchronous test wrote a plain muted sentence. It now renders the existing Northbound success notice with **Hardcover Catalog Connected** and explanatory copy; saved-token test/save messages use matching language. Catalog-only scope guidance is retained. |
| Group OAuth investigation | Reader OAuth lifecycle, consent and refresh are Reader-owned. A Group variant needs new ownership and catalog-only authorization design, so it is deferred for possible v1.1 consideration. |
| Date/time defaults | Empty combined native pickers allowed the browser to introduce the current time. All application date/time widgets now use paired native date and time controls, with a blank date and `00:00` time. Existing local times, submitted corrections and Django timezone/DST validation are preserved. Schedule, Challenge edit, Game and checkpoint fields share this widget; new checkpoint rows inherit it without JavaScript. |
| Editing duration | Settings always rendered the duration, and the server required it for all modes. The UI immediately shows/enables it only for the timed policy, including initial page load. Server validation still enforces 1–720 hours for timed editing; other modes ignore submitted hours and retain the saved/default duration. |
| BOTM badge | The entity-card flex column stretched its direct badge child; Games navigation cards already aligned children to the start. Direct entity-card pills now align to the start, preserving the width of card content/actions. Sibling settings navigation and lifecycle cards do not share the defect. |
| Smart BOTM entry | BOTM exposed separate search/import controls. One Find Book input now accepts title, author, ISBN or Hardcover book/edition URL. Shared server routing is reused with TBR, which already had the unified flow. Edition selection, signed selections and manual fallback remain intact. BOTM uses Group credentials; TBR uses Reader credentials. Submission entry retains its existing controls. |
| Book Details spacing | BOTM inherited the section-heading 54px outer-section margin inside a form card. Its heading now uses the existing submission Book Details zero-top-margin treatment. TBR uses its own correctly spaced registration heading and requires no change. Card padding is unchanged. |
| Stable Docker tags | Main alone previously explicitly selected latest. Exact stable `vMAJOR.MINOR.PATCH` tags now select latest too; metadata-action automatic latest is disabled. One build publishes release, latest and SHA tags together, so they reference the same output image digest. Prereleases do not promote latest. |

### Validation

- 77 focused existing tests passed: Challenge settings, Personal TBR registration,
  BOTM management, Games management UI and progress checkpoints.
- 10 new tests passed: blank and saved times, timezone/DST behavior, duration
  policies and bounds, Group catalog routing/credential ownership, token success
  copy and the actual workflow tag-selection script (11 branch/tag cases).
- Django system check and `makemigrations --check --dry-run` passed; no migrations
  introduced or applied to the local application database.
- Workflow YAML parsed with Ruby YAML; JavaScript passed `node --check`;
  `git diff --check` passed.
- Browser component preview verified initial and dynamic duration visibility,
  date-only selection retaining midnight, preservation of an existing 17:45,
  equal content-width BOTM/Games badges, and card heading spacing.
- Browser BOTM preview verified edition-link selection, locked pages, clearing to
  manual entry and Enter-key search using fake provider responses.
- Manual acceptance of the complete v1.0.1-dev implementation is complete.
- Release publication uses the repository container workflow. Published image
  digests and CI evidence are recorded in the public GitHub release notes.

### Manual acceptance coverage

Taylor confirmed acceptance of the current v1.0.1-dev implementation before release.

1. Test a Group token and Save and Test; confirm the success treatment and scope
   guidance. Check an invalid token retains a useful error.
2. On Challenge schedule, Game and checkpoint forms, select a new date: time should
   be midnight. Save a non-midnight time and reopen it; confirm preservation in the
   Group timezone. Test a newly added checkpoint row as well.
3. Toggle all three Reader Answer Editing policies. Only the timed policy should
   display duration, and invalid/missing timed hours should be rejected on save.
4. Compare BOTM and Games Enabled/Disabled badges on Challenge Settings, and Book
   Details heading spacing on BOTM add-book and submission forms, on mobile/desktop.
5. In BOTM, try a title, author, ISBN, book URL and edition URL. Select the intended
   edition, save, and try Clear and Enter Manually. Confirm TBR still works through
   its Reader connection and manual fallback.

The user's clean-install checks (public pull after removing stale GHCR auth,
SQLite initialization, health, setup and live Reader OAuth) are accepted context,
not newly rerun checks. Stale saved GHCR credentials are not a Northbound defect.
Password reset, email providers, Group OAuth redesign and other v1.1 features are
outside this batch. No v1.1 or additional roadmap work is included.

### Files changed

- `.github/workflows/container.yml`
- `README.md`
- `core/forms.py`, `core/widgets.py`, `core/views.py`
- `core/test_fresh_install_polish.py` (new, explicitly added despite the existing test ignore rule)
- `static/js/app.js`, `static/css/theme.css`
- `templates/base.html` (asset cache versions), `templates/core/botm_book_form.html`
- `docs/SELF_HOSTING.md`, `docs/HARDCOVER.md`, `docs/HOST_GUIDE.md`, `docs/RELEASE_NOTES.md`
- Canonical sibling `Reading Log Roadmap/ROADMAP.md`
