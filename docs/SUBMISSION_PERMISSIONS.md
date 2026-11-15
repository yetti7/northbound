# Issue #7 Phase 7C: delegated submission management

The Phase 7B workspace and existing role hierarchy remain in place. Hosts,
Floaters and Team Leaders now use existing scoped review authority for exceptional
BOTM/TBR credit previews and confirmation, as well as legacy BOTM adjudication.
No Challenge-wide synchronization, configuration or reconciliation authority is
added. Group Owners retain their existing correction authority; Group ownership
alone still does not confer ordinary book-review staffing.

## Server-side scope

`can_manage_credit` composes `can_review_submission` with the existing unrestricted
Group Owner authority. Platform Administrators are platform-wide. Non-participating
Hosts and Floaters are Challenge-wide; participating Hosts remain restricted to
current Team assignments and active target enrollment. Team Leaders retain their
assigned-Team scope. Active authentication and Group membership are required where
applicable; `can_review_submission` explicitly rejects inactive accounts. Existing
Host precedence over other staffing remains intact. Readers keep their own eligible
pending edits and requests, without staff authority.

Preview and apply reload actors and submissions and recheck current authority.
Team transfers, ended staffing, revoked memberships, Host enrollment changes and
lifecycle changes invalidate pending operations. A signed token carries no durable
authority. Existing service checks continue to reject ineligible/departed recipients,
cross-Challenge BOTM destinations, cross-Reader TBR destinations, duplicate credit,
unsupported frozen-award changes and protected-record mutations.

Exceptional assign/reverse/restore retain signed, actor-bound, stale-safe previews,
required confirmation, reasons, conflict acknowledgment and atomic audit generation.
A credit held by another submission cannot be stolen: reverse the incorrect credit
through its own supported preview, then assign only if the existing service permits.
Frozen completion-award reversals remain blocked; this phase adds no historical
award repair. The evidence key `authority=administrator` is the existing exceptional
service marker, not a claim about the actor's role; consumers and history retain it.

## Approved Review safeguard

Investigation confirmed that ordinary Review could change an approved book's pages
and Theme decisions without a correction-impact preview. Approved Review now makes
status, approved pages and Theme decisions read-only, rejects forged POST changes,
and directs scoring corrections to Edit Book. Its signed review service rejects
approved qualification/prompt decisions; use Qualifications or the existing signed
correction flow. Pending approval/rejection and existing eligible ordinary
self-approval remain unchanged. Immersive independent review remains unchanged.

The authorized review service also rejects approved scoring changes independently
of form handling. Notes and verification-link updates save only those fields and
review attribution, with an audit, without resynchronizing scores or qualifications.
Trusted low-level scoring primitives remain unchanged; public staff operations use
the authorized review or signed correction services.

Limitation: the existing correction service preserves submission status, so changing
an approved book back to Pending/Rejected is blocked through ordinary Review. No
new status-correction workflow is introduced. Existing void/restore remain separate
operations with their own rules. A future approved-status correction would need a
signed impact preview that includes all dependent qualification/award changes.

## Workspace and compatibility

Existing Qualifications buttons follow the same live predicates as their POST
services. Leader/Floater legacy BOTM POSTs return to their accessible Qualifications
section rather than the Host-only BOTM history list. All existing signed-origin,
preview, form-error and Reader navigation behavior remains in place. There is no
layout redesign, schema migration, Discord behavior change or background worker.

## Acceptance evidence

Twelve synthetic delegation tests cover all delegated roles, both credit domains,
preview/apply/reverse/restore, explicit reassignment, duplicate rejection, forged
approved-scoring POSTs, notes-only scoring preservation, book preview confirmation,
stale/no-op rejection, and authority loss. Existing focused modules cover scoped
ordinary review/self-approval, Reader editing/requests, Theme corrections, Immersive
independence, frozen awards, correction atomicity, Discord outbox rollback, workspace
adapters and signed navigation. An initially misnamed test-module invocation was
corrected; the actual Discord correction module passed. The existing row-lock
concurrency test remains skipped on SQLite. Django check and migration detection
pass; no migration exists for this phase.

Dev-only source deployment replaced seven runtime files, using the same pinned
image and read-only mounts. A private Dev rollback backup was taken first. Health
returned HTTP 200, source hashes matched, and database/media fingerprints matched
the pre-deployment snapshot. Authenticated existing Platform Administrator, Group
Owner, Host, Floater and Team Leader GETs rendered all five workspace sections and
authorized adapters with SQLite query_only enabled. No live POST or acceptance
record change was made. Automatic Discord global gates, scheduled delivery, legacy
digests and the Challenge scheduler remain disabled; destination preferences are
preserved and no background worker or real delivery was started.
