# v1.0.7 hotfix review and historical TBR assessment

Status: implemented for review; no release or production repair performed.

## Corrections

- Attach the Challenge to BookSubmissionForm's instance before model validation.
  Missing/invalid completion dates remain field errors; dates outside the Challenge
  and missing Challenge schedules render useful form errors. Model validation now
  guards an absent completion date before comparing dates.
- Handle expected final submission model validation errors before any writes.
  All submission, scoring, BOTM, Personal TBR and audit writes remain within the
  existing atomic transaction. Exceptions from downstream services still escape
  and roll back that transaction.
- Invoke existing Personal TBR synchronization on creation. Pending submissions
  remain ineligible; matching algorithms and award services are unchanged.
- With DEBUG=False, send django.request errors to stderr using a restricted
  formatter: exception class and stack filename/line/function locations only.
  Do not output exception values, log messages/arguments, source lines, request
  bodies/headers/query strings, locals, or stack_info. Disable propagation for
  this logger to avoid duplicate or less restricted output through parents.
  The tradeoff is less diagnostic detail; exception values are deliberately absent.

## Read-only historical report

Production data has not been accessed. A populated GuildWars inventory and actual
bonus totals are therefore unavailable; the local development database is not
production evidence. No historical submissions have been reconciled.

The new management command produces JSON using SELECT queries only:

```text
python manage.py report_missing_personal_tbr_matches --group guildwars --month 2
```

Run from the reviewed source with an approved read-only database snapshot and
appropriate application configuration. Do not invoke the container startup script
(which performs migrations), synchronization commands, or a repair operation.
The command itself never calls matching synchronization, scoring, or award writes.

The output identifies submission, participant and candidate TBR book IDs for
approved, unremoved submissions belonging to active eligible Readers, active
enrollments and confirmed TBRs with TBR enabled. It uses the existing deterministic
candidate selector. It deliberately includes only submissions with no TBR match
records; pending/rejected/nonqualifying existing records require separate review.
No titles, reader names or credentials are included in output.

Each row identifies a unique uncredited candidate, ambiguous candidates needing
review, or a duplicate/already credited book. Projected totals process missing
submissions in ID order and count each book once. Per-reader output includes:

- Current and projected completed-book counts.
- Potential new per-book bonus pages at current configuration.
- Whether the nine-book completion condition could become satisfied.
- Existing completion award ID and potential completion bonus, using its frozen
  amount when present, otherwise the current configured amount.

These are projections, not authorization or an executable repair plan. A live
read is not a consistent database snapshot. Before proposing repair, inspect
existing match decisions, durable provenance, frozen completion sets and amounts,
award effective dates, and historical checkpoint implications on a snapshot.
Report mismatches instead of overwriting established awards or snapshots.
New missing per-book awards would use submission completion dates; a newly created
full-completion award normally uses reconciliation time's local date. That timing
must be explicitly reviewed before any production repair is proposed.

## Validation

Result: 49 targeted tests passed in 12.291 seconds; Django system checks and
`git diff --check` passed. Coverage includes the new hotfix tests, existing
SubmissionWorkflowTests and existing Personal TBR matching, per-book rewards and
completion test modules. Tests ran
against a temporary in-memory SQLite schema with migration files disabled, never
against the local or production database. No schema migration files are changed.

Remaining incident limitation: the production HTTP 500 exception has not been
captured. The deterministic date failure is fixed, but this does not prove every
reported failure had that cause. Production stderr diagnostics will help identify
any different exception after an independently approved release/deployment.

## Completion-pass implementation

The original hotfix above remains intact. Completion-date guidance, shared cover
fallbacks, and controlled administrative reconciliation are now implemented.
Hardcover TBR/BOTM metadata refresh and display-metadata separation are deferred
to v1.0.8 and are not v1.0.7 release blockers. No refresh endpoint or write
shortcut has been introduced. Authorized submission editing and its immediate
scoring corrections are also deferred to v1.0.8.

### Date guidance and cover rendering

BookSubmissionForm supplies actual inclusive Challenge dates as help text and
HTML min/max attributes. Out-of-range dates produce a completed_on field error;
the model remains authoritative. Missing schedules do not produce invented limits.

A shared book_cover.html include, safe URL filter and book_covers.js helper handle
missing/malformed URLs, load failures and already-failed cached images. Cover
children cannot contribute intrinsic dimensions to card tracks. Existing 2:3
frames, completed overlays and pending labels remain in Personal TBR, BOTM,
participant-detail, Challenge preview and registration/search-preview surfaces.

### Controlled reconciliation

The original report_missing_personal_tbr_matches command remains read-only and
approximate. The new reconcile_personal_tbr command supplies a separate, validated
scoring preview. It checks an active Host, Group Owner or Platform Owner account;
Floater/Team Leader review scope does not confer administrative repair authority.
For this release reconciliation is restricted to Active Challenges and SQLite
configured with IMMEDIATE transactions.

Preview copies a consistent SQLite image (including WAL contents) into a private
temporary directory and runs the existing synchronizer in an isolated subprocess
configured exclusively to that copy. It never simulates writes on the source and
never runs migrations. The temporary database is removed after preview. Preview
must run outside a write transaction.

The preview identifies Readers/books, before/after completion counts, exact new
match/provenance/award effects, amounts and effective dates. Operational timestamps
are assigned at Apply; they are not represented as exact future timestamps.

Example commands for a separately authorized future operation (not run against
production during this work):

```sh
python manage.py reconcile_personal_tbr --group guildwars --month 2 --actor ADMIN_USERNAME --output /tmp/guildwars-tbr-preview.json
python manage.py reconcile_personal_tbr --group guildwars --month 2 --actor ADMIN_USERNAME --apply --preview-file /tmp/guildwars-tbr-preview.json --confirm 'RECONCILE guildwars 2'
```

The output file is created with mode 0600 and will not overwrite an existing file.
Review its readable plan before applying. Apply checks that the readable plan
matches its signed counterpart, administrator and Challenge identity, exact typed
confirmation, 30-minute expiry, relevant source-code hashes, and database state.
The state digest is deliberately conservative: unrelated core data changes can
invalidate a plan and require another preview.

Apply rechecks candidates and reruns the services in one transaction. Actual
scoring/qualification effects must match the preview, including effective award
dates; a midnight date change therefore rejects and rolls back the repair.
Existing awards/provenance cannot be rewritten, historical checkpoints cannot
change, and unrelated new bonuses are refused. Successful Apply records an audit
event. A replay or concurrent attempt cannot award duplicate credit; lock failures
require a fresh preview rather than an automatic retry.

Missing per-book bonuses use original completion dates. New nine-book completion
awards use the local reconciliation date. No automatic deployment reconciliation
exists. The old immediate-apply web endpoint returns 403 for unauthorized users
and 409 for authorized administrators, and its Synchronize button is removed.

### Metadata refresh deferred to v1.0.8

PersonalTBRBook.clean() and BotmBook.clean() derive normalized matching identity
from title_snapshot/author_snapshot. These same snapshots drive display, and
locked TBR / BOTM history guards deliberately protect them. There is no independent
display-title/author/page layer. Updating snapshots or bypassing those guards
would weaken the approved separation of display refresh from matching evidence.

No shared CatalogBook/CatalogEdition writes, refresh API calls, model-lock bypasses
or refresh permission shortcuts were added. The entire refresh feature is deferred
rather than implementing a partial operation that could be mistaken for the
approved full refresh. A follow-up needs a separately approved display-metadata
representation and its schema/guard implications. Existing artwork fallbacks work
without that change. Refresh-specific regression cases remain deferred with it.

### Validation and release assessment

- 86 targeted Django tests passed in 21.788 seconds, using temporary in-memory
  schemas with migration files disabled. This includes the original hotfix,
  submission workflow, covers/presentation, matching/rewards/completion and new
  administrative reconciliation tests.
- New tests cover date help/limits/inline errors; cover validity and overlays;
  SELECT-only source queries during preview; confirmation/signature/stale plans;
  command-file privacy; duplicate and concurrent repairs; nine-book awards;
  original per-book dates and reconciliation completion dates; rollback after
  writes; preservation of existing awards; and unchanged checkpoint snapshots.
- Both book_covers.test.js and personal_tbr_registration_helpers.test.js passed.
- Django system checks and git diff --check passed.
- Synthetic headless Chrome inspection at exact 1200px and 390px viewports found
  no horizontal overflow. All four cover states had equal 2:3 frames (112x168 and
  80x120 respectively), valid artwork displayed, failed/missing/malformed artwork
  used placeholders, and completed artwork overlays loaded. Screenshots were
  inspected. No production data was used.

Files changed in this continuation:

- core/forms.py, core/permissions.py, core/views.py
- core/tbr_reconciliation.py
- core/management/commands/reconcile_personal_tbr.py
- core/templatetags/__init__.py, core/templatetags/book_covers.py
- core/test_hotfix_completion.py
- static/css/theme.css
- static/js/book_covers.js, static/js/book_covers.test.js
- templates/base.html
- templates/core/botm_list.html, templates/core/challenge_participant_detail.html
- templates/core/month_detail.html, templates/core/personal_tbr_detail.html
- templates/core/personal_tbr_match_list.html
- templates/core/includes/book_cover.html
- templates/core/includes/challenge_tbr_fields.html
- templates/core/includes/challenge_tbr_script.html
- docs/V1_0_7_HOTFIX_REVIEW.md

The remaining original uncommitted hotfix files are retained. No production data
was read or repaired, no release actions occurred, and no infrastructure or schema
changed. The approved v1.0.7 scope passed final release-readiness review. Metadata refresh,
display-metadata separation, authorized submission editing and scoring corrections
through editing are deferred to v1.0.8; these deferrals do not block v1.0.7.
Actual production 500 causality remains unconfirmed until the improved logging
supplies evidence.
