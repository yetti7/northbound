# v1.0.8 release amendment record

Status: first amendment published; the All Submissions follow-up below is prepared for PR review only.

## Original publication

- GitHub release: https://github.com/yetti7/northbound/releases/tag/v1.0.8
- Original release commit: `639d80a220146c063dea9f704e3f8125979dfc1b`
- Original annotated tag object: `b2f3f8118fd19981848741aa3a58e4b16f918211`
- Registry observation (UTC): `2026-11-04T22:13:56.381986+00:00`
- Original `ghcr.io/yetti7/northbound:v1.0.8` index: `sha256:cceedabe59e1fe7425d1ab95ee59ac37090097a574d72ac72fed83665d24fa80`
- Original `latest` index: `sha256:cceedabe59e1fe7425d1ab95ee59ac37090097a574d72ac72fed83665d24fa80`
- Original linux/amd64 descriptor: `sha256:8cd05f86dfd583250946011dceee5a490a5b669df944531b6143ddb06fe1c4e4`
- Original linux/arm64 descriptor: `sha256:1c6273cfccc158f273c9d52159dee6161c84f6671cd65e866e2f331ff36848b3`
- Original unknown/unknown descriptor: `sha256:6f1f31fe5244cc5fafbcf46065984f9e3b4a27ded6566c3e88422861eb47dee8`
- Original unknown/unknown descriptor: `sha256:de0bc87d1c4c819bc33c089019b7b8ea026736cc3e1677e263444e0eda947781`

The two unknown/unknown descriptors are associated attestation manifests.

## Approved scope

- Expanded submission void permissions with current Team Leader team scope and existing lifecycle restrictions.
- Participant submission table improvements and accessible floating action menus.
- Platform Owner deletion shortcut to the existing protected recovery workflow.
- Compact Platform Administration Groups overview.

No schema, version, scoring-engine, recovery-engine, or CI changes.

## Amendment feasibility and remaining gates

The existing GitHub release reports `immutable: false`. Repository rulesets returned an empty list; classic main protection reported "Branch not protected". The legacy tag-protection endpoint returned 404 and is not evidence of an active tag rule. Recheck protections before the separately approved tag replacement.

The existing workflow builds PR images without publication. Main pushes publish `latest` with commit-based version metadata; stable-tag pushes publish the named version and `latest`, with tag-based version metadata. Consequently merging main can change `latest` before the replacement tag build finishes. No workflow changes are proposed.

After CI passes and separate approval is received: merge the PR, record the new main commit here, replace the annotated v1.0.8 tag, allow the existing workflow to publish, verify amd64/arm64 digests and version metadata, and append the notes below to the existing release. Do not delete the GitHub release.

First amended release commit: `cee7ab3e41ce9680f08950899f27b04ee700c395`. Its observed image digests are recorded in the follow-up baseline below.

Consumers with cached images or digest pins will retain the original image until explicitly updated. Preserve this record and the original commit/digests. Production deployment is a separate step.

## Preparation validation

54 focused Django tests passed in 43.454 seconds. Django system checks, migration consistency, JavaScript syntax, and git diff whitespace checks passed. No full-suite rerun or production access was performed.

## First amendment release-note addendum (published)

### Submission management and administration update

- Challenge staff, Group Owners, and Moderators can void incorrect submissions within their authorized scope; Team Leaders remain limited to their current teams.
- Cleaner submission tables include a compact, keyboard-accessible Actions menu. Platform Owners can open the existing confirmed permanent-deletion workflow.
- The Platform Administration Groups overview is more compact and easier to scan.

This amends v1.0.8; no additional database migrations are required. Existing scoring and recovery safeguards are preserved.

## All Submissions follow-up amendment preparation

Status: pending a main-targeting PR, passing CI, and separate merge/publication approval. Version remains v1.0.8; no new release or version is planned.

### Current published baseline

- Current v1.0.8 target: `cee7ab3e41ce9680f08950899f27b04ee700c395`.
- Current annotated tag object: `87209dc40178515f240400f5a6343a79bc15437d`.
- Registry observation (UTC): `2026-11-04T23:10:52.235147+00:00`.
- Current `v1.0.8` index: `sha256:87a4ecf64e15460714a539b2d75abb7dc3d01012ae7f02f3e1635e70a83bc927`.
  - linux/amd64: `sha256:86b6fe52040bf1ba86cc7af8c4fd93148a5b27bb5f43ed9a6850c5040ed3faac`; `NORTHBOUND_VERSION=v1.0.8`; revision `cee7ab3e41ce9680f08950899f27b04ee700c395`.
  - linux/arm64: `sha256:3f49e943ffbfc5285cc97227c00a8ea1c86656c370a4fb68332a087680ef5d67`; `NORTHBOUND_VERSION=v1.0.8`; revision `cee7ab3e41ce9680f08950899f27b04ee700c395`.
- Current `latest` index: `sha256:a4d205c90a3b8e780dcbf4ee34f2c6e79a02b658c4bc939345d055c63a915a04`.
  - linux/amd64: `sha256:dd049ec0070d86c1394844cdfdb90805881c71c4adc2ae1d446bdfa85ef3e486`; `NORTHBOUND_VERSION=cee7ab3e41ce9680f08950899f27b04ee700c395`; revision `cee7ab3e41ce9680f08950899f27b04ee700c395`.
  - linux/arm64: `sha256:3848501aeb132ef2cb33d547dbabe031758dd05707ba73bd9c39f5760ee6497f`; `NORTHBOUND_VERSION=cee7ab3e41ce9680f08950899f27b04ee700c395`; revision `cee7ab3e41ce9680f08950899f27b04ee700c395`.

Both versioned architectures identify the current release commit and v1.0.8. The observed latest images have the same source revision but commit-based version metadata and different digests: the main publication finished after the tag publication. This is the existing workflow race described above. Do not assume latest equals the versioned tag; verify both before a later approved publication is declared complete. No CI change is included.

The existing release ID remains 434409105, public/stable and mutable; current repository rulesets are empty. No release artifacts are modified in this preparation.

### Approved follow-up scope and validation

Challenge-wide All Submissions table, live search with 300 ms debounce, removal-flag-based Voided filtering, server-side pages of 25/50/100, dynamic floating action menus, accessible Reader ellipsis and single-line status badges, neutral heading, and one visible counter with screen-reader announcements. Existing viewer scopes and action permissions remain unchanged; the participant table is unchanged. View as User and Act as User remain deferred.

Reuse the latest 28 passing focused tests and the previously passing search, pagination, ordering, status, permission, and submission-management checks. Reused synthetic browser checks cover live updates, cancellation, stale responses, focus/cursor preservation, errors/retry, the single visible counter, and desktop/narrow layout. No application code changed after those validations. Fresh Django, migration consistency, JavaScript syntax, and diff checks passed. No full-suite rerun is needed.

User-facing draft notes are under v1.0.8 in RELEASE_NOTES.md. After the follow-up PR passes CI and separate approval is received, merge it, record its merge commit in the replacement annotated tag and release amendment notes, replace only v1.0.8, let the established workflow publish, and verify both architectures and latest/version metadata. Preserve every prior commit and digest recorded here. Production deployment is separate.
