# Reader submission management (Phase 7A.2)

The Reader's existing Challenge book list now links to an owned submission page
with Qualifications and, when eligible, Edit Submission. Staff correction pages,
review scopes and ordinary-book self-approval permissions are unchanged.

## Pending edits

An active Reader with an eligible enrollment may edit only their own unremoved
Pending book in an Active or Finalizing Challenge. Late/restored historical credit,
reviewed Theme evidence, existing provenance and awarded qualification/Immersive/
prompt credit fail closed to staff correction. Approved and Rejected books cannot
be edited through this interface. Group, ownership, enrollment, Floater and lifecycle
checks are repeated under the Challenge/submission locks using freshly loaded users.

Title, author, format, pages, completion date and applicable Themes reuse
BookSubmissionForm validation. Start date and notes remain unchanged; only verified
catalog selection may update its reference link. Signed Hardcover selections reuse the
existing work/scoring-edition consistency validation. The subclass always clears
creation-time approval and approved pages before model validation: edits remain
Pending with manual review required. Existing catalog associations and provider
metadata are retained unless explicitly removed or replaced; new selections use
verified catalog metadata. Removing an association does not refresh provider data.

Original acceptance snapshots, original BOTM/TBR intent, owner, Challenge, Team,
review attribution and audits are never replaced. Theme removals retain claim
history. New/changed qualification selections create review requests. Removing an
unreviewed current selection records Reader withdrawal on its relationship and a
separate audit, preserving original intent. Withdrawn/rejected targets cannot be
reopened through this Reader path. Reviewed decisions require staff correction.

Signed actor/submission versions expire after 30 minutes and cover submission,
qualification, Theme, participation, Team and lifecycle state plus audit revision.
The locked save checks the version before constructing updates. Duplicate/no-op
replays and reviewer/request/Team changes invalidate old forms. Failed saves roll
back fields, claims, matching, audits and observed Discord outbox records together.
No provider network call or approved correction engine is introduced into this save.

## Post-submission qualification requests

No schema migration or parallel request model is required. Existing BotmMatch and
PersonalTBRMatch already provide a unique submission/target relationship, domain,
status, timestamps, reviewer and decision metadata. `decision_evidence.reader_request`
adds requesting user, domain, signed target evidence, request creation timestamp and
identity fingerprint. An audit on BookSubmission makes the request and decision
visible in the Reader's scoped timeline. Original selected_* and reader_intent stay
immutable. Review preserves this request provenance alongside reviewer evidence.

Readers may request on eligible Pending/Approved books in Active/Finalizing
Challenges. Candidate presentation reuses Phase 7A.1 conservative suggestions and
the live identity evaluator; at most three candidates per domain are offered.
Targets remain scoped to active BOTM or the Reader's confirmed Personal TBR. Work,
volume/adaptation conflicts, duplicates, prior decisions, Bingo/TBR conflicts,
departure, late approval, restored historical credit and closed lifecycles fail
closed. There is at most one active explicit Reader request per submission/domain;
existing relationship uniqueness additionally prevents duplicate target requests.

Recording a request writes only its match relationship and audit. It neither
synchronizes scores nor modifies the approved book. Even strong explicit requests
remain unqualified during later automatic matching until authorized review.
An audited active request supersedes original selection for candidate routing;
it does not rewrite the historical selection itself.

## Existing reviewer workflow

The existing submission review page shows pending Reader requests. Reviewers can
approve the ordinary book and confirm its request through the existing matching
checkbox, or decide qualification separately once the book is Approved. Pending
requests can be rejected separately. Signed stale-state checks, current review
scope, identity fingerprints, feature/participation/duplicate checks and existing
reward services remain authoritative. No independent-review rule is added to
ordinary submissions; Immersive's separate policy is unchanged.

Qualification decisions preserve book identity, pages, completion date, Theme
records and every pre-existing frozen completion award/set/date/amount. Existing
services may establish a new completion award through normal rules; rewriting an
existing frozen award fails and rolls back. This is normal qualification review,
not an exceptional credit override or an approved book correction. Existing signed
correction previews remain necessary for those separate operations.

Changed/stale request identity cannot be approved through this path. A prior
rejection/withdrawal cannot be reopened by the Reader. Exceptional evidence or
historical/frozen changes require the existing authorized staff workflow. No
historical reconciliation, background worker or Discord delivery is activated.
