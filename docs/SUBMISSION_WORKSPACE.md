# Unified Submission Management (Issue #7, Phase 7B)

The primary staff destination is submission-manage with section=overview, edit,
qualifications, review or history. Five ordinary links support deep links, keyboard
navigation and no-JavaScript use. A single compact saved-record summary shows
identity, Reader/Team/Challenge, status, format/pages and qualification state.
Summary data is reloaded separately from bound forms so failed edits never appear
to have changed the saved book. Reader notes remain limited to existing review
access. The wider CSS is scoped to this workspace; other forms retain their limits.

## Sections

- Overview shows evidence, warnings and the next appropriate action. Pending
  review links to Review & Scoring; approved book corrections go to Edit Book.
  Void/restore still open dedicated confirmation pages.
- Edit Book opens the existing compact correction form with Book Details and
  Pages & Completion expanded. Themes and verified Hardcover association retain
  their existing controls. Manual format correction is unsupported and shown
  read-only; verified edition changes keep their existing format behavior.
  Approved corrections still generate signed impact previews. Focused and
  advanced correction adapters remain available without duplicated book summaries.
- Qualifications shows current BOTM/TBR decisions, Reader requests, available
  matching evidence and independent Immersive state. Foundation candidate
  confirmation, Reader-request review, existing credit reversal/restoration and
  exceptional assignment are labeled separately. Existing-credit links preselect
  only a scoped target/operation on GET; no decision or write is made by navigation.
  Existing permission and eligibility checks still govern every POST.
- Review & Scoring retains ordinary book/page/Theme review and matching tokens.
  Current saved scoring is visible separately from proposed form values. Browser
  submissions return to this section. Older direct review POST clients without
  workspace_return keep their original queue return behavior.
- History uses submission_timeline with its existing authorization and event
  filtering. Pagination retains the active section and signed list origin.

## Operation and compatibility boundaries

GET section adapters reuse existing views. Forms explicitly target their original
POST endpoints, not a combined Save handler. The primary Manage route remains GET
only, with its original access guard. Existing review, editor, focused correction,
advanced correction, credit and history URLs remain usable. Invalid forms render
in the affected section with bound values and original tokens. Previews are never
redirected away or converted to normal saves.

Exceptional credit previews and Immersive approve/reject/withdraw/revoke
confirmations remain distinct operations. Staff Immersive status is consolidated
into Qualifications; Reader Immersive pages remain simple. Void/restore retain
separate services and confirmation routes. Workspace-initiated state actions return
to Overview or the existing authorized History fallback when Manage access no
longer applies. Void validation errors retain their reason instead of navigating
away. No write transaction or service authorization predicate is widened.

All section links and form actions preserve only valid, user-bound, same-context
signed navigation origins. Unknown section names fail closed. Unsaved-change
warnings compare each form separately, including silent edition-field changes and
server-rendered preview/error state. Submitting a form suppresses the warning for
that form only; dirty independent forms still warn. Full-page navigation always
works without JavaScript. Focus indicators and wrapping section navigation support
375/440/768/1280px layouts without changing global form widths.

## Permissions and unchanged policy

submission_workspace.py composes existing can_edit_submission,
can_review_submission, can_manage_credit and Immersive predicates for presentation.
There are no role/scope changes. In particular Team Leaders/Floaters gain no
exceptional credit access in this phase. Participating Host restrictions, Team
transfers, departed Reader checks and ordinary self-approval remain in existing
services. Immersive's independent-review requirement remains separate.

Reader details, pending edits, post-submission requests and TBR artwork workflows
are preserved. Ordinary Readers cannot enter staff management or its POST controls.
Scoped Reader history remains the existing simpler page. Staff who can read
history but do not pass the existing Manage guard retain that authorized legacy
history page rather than receiving new management access.

Known policy inconsistency, intentionally unchanged: existing ordinary Review can
change approved pages without the correction editor's impact-preview sequence.
The workspace directs approved book corrections to Edit Book and preserves its
preview safeguards; it does not silently change the Review policy or route book
corrections through Review. Changing that policy requires a separate decision.

No scoring, matching, bonus, frozen-award, audit, outbox or lifecycle calculation is
changed. No model migration, dependency, image rebuild, worker or Discord
activation is required. Phase 7C permissions and subsequent Discord work remain
outside this implementation.

## Phase 7B acceptance evidence

Focused regression runs cover workspace adapters, correction/association services,
qualification requests and credit, scoped review, Reader pages, Themes, Immersive,
void/restore, signed navigation and Discord correction transactions. The workspace
module adds 19 tests; its JavaScript event harness covers dirty-form and preview
warnings without network calls. Django checks and migration detection pass with
no migrations required.

Browser checks used authenticated Dev GET renders with SQLite query_only enabled,
and synthetic approved-preview/error renders from a disposable local test database.
All five sections plus preview/error states fit 375, 440, 768 and 1280px frames
without document overflow. Keyboard Tab exposed a visible 3px focus outline.
Dirty-edit navigation was blocked in the in-app browser and worked again after
reverting the field; this browser did not expose the native beforeunload dialog
through its dialog API, so the warning contract is also verified by the event
harness. No live form was submitted or acceptance record changed.

The Dev deployment replaces only 27 runtime files, retaining the existing image
and read-only source mounts. Health returns HTTP 200. All 67 database table
fingerprints, migrations and 40 media files match the pre-deployment snapshot.
Global automatic Discord delivery, scheduled delivery, legacy digest and Challenge
scheduler remain disabled. Saved destination preferences are preserved, and no
background worker or real Discord delivery was started.
