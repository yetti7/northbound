# v1.0.8 release amendment record

Status: prepared for PR review; existing release, tag, and images are unchanged.

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

Amended release commit and image digests: pending separate approval and publication.

Consumers with cached images or digest pins will retain the original image until explicitly updated. Preserve this record and the original commit/digests. Production deployment is a separate step.

## Preparation validation

54 focused Django tests passed in 43.454 seconds. Django system checks, migration consistency, JavaScript syntax, and git diff whitespace checks passed. No full-suite rerun or production access was performed.

## Prepared release-note addendum (not yet published)

### Submission management and administration update

- Challenge staff, Group Owners, and Moderators can void incorrect submissions within their authorized scope; Team Leaders remain limited to their current teams.
- Cleaner submission tables include a compact, keyboard-accessible Actions menu. Platform Owners can open the existing confirmed permanent-deletion workflow.
- The Platform Administration Groups overview is more compact and easier to scan.

This amends v1.0.8; no additional database migrations are required. Existing scoring and recovery safeguards are preserved.
