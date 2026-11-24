## Reporting and Team Rosters

- Team Total now follows reporting dates, bonus selection, Theme, Personal TBR, BOTM, and authorized Reader search filters. The card shows one total labeled **Base Pages** or **Base + Bonus**.
- Historical contributions retain their established Team attribution. Sorting and optional informational columns do not change totals.
- Active Floaters have Challenge-wide reporting visibility without Reader enrollment or Team assignment.
- Reporting controls consistently respect existing data visibility. Protected Reader scores remain private, including when searching a roster.
- Desktop roster filters stay within the viewport while preserving the mobile interface and keyboard navigation.

## Review Queue and Moderation

- The redesigned Review Queue groups work by Team, Reader, and submission while retaining individual pending-review counts.
- Hosts can use Restricted or Unrestricted review access. Existing legacy Host scope is preserved until an explicit choice is made.
- Authorized reviewers may review their own Immersive Reading requests within their existing review scope.
- Theme and other qualification decisions remain independent. Rejecting a Theme does not reject its parent book or remove its Approved Pages.
- Eligible initial Bingo and Scavenger Hunt claims can be reviewed on already-approved books without changing book approval or bypassing correction safeguards.

## Reader Notifications

- New explicit rejections of books, Themes, Immersive Reading, Bingo, Scavenger Hunt book claims, and Scavenger Hunt photos require a meaningful Reader-facing reason.
- Private rejection notices appear in Needs Attention with the affected item, Challenge, reason, date, and available next steps.
- Readers can acknowledge their notices without changing the underlying decision or scoring. Supported, reliably linked corrections and resubmissions can resolve notices automatically.
- Historical rejections are not backfilled. Unsupported correction workflows continue to offer viewing and acknowledgment rather than a misleading correction action.

## Book Management

- Consolidated Book Management shows relevant book, Theme, Immersive Reading, Bingo, and Scavenger Hunt information with authorized actions and links.
- Team Books presentation is clearer and more consistent with the surrounding interface.

## Bingo and Scavenger Hunt

- Configured Challenges can use Bingo and Scavenger Hunt workflows, including book qualifications and Scavenger Hunt photo review, subject to existing lifecycle and eligibility rules.
- Newly configured future Challenges use the same workflows; availability is no longer blocked by the former application-level readiness gate.
- Existing prompt rules, completion bonuses, scoring provenance, and correction safeguards are preserved.

## Branding

- Group, Challenge, and Team branding support independent display modes for banners, logos, and overlays.
- Branding controls and previews more clearly reflect the selected presentation.

## Discord Notifications

- Global Discord notification control is applied consistently alongside existing Group and destination settings.
- Scheduled notifications support replacement cleanup, with delivery tracking that protects current messages and unrelated destinations.
- Existing destinations retain their cleanup preferences during upgrade; administrators can configure the available cleanup options explicitly.

## Administration and Usability

- Group Members displays are cleaner across desktop and mobile.
- Needs Attention navigation includes personal rejection notices alongside existing reviewer work and retains role-appropriate visibility.
- Floater participation restrictions, Team Leader scope, Host review boundaries, and administrative permissions remain intact.

## Technical Improvements

- Rejection notices use persistent event and target identities to prevent duplicates and support private, idempotent acknowledgment.
- Reporting reuses authoritative scoring and historical attribution rather than introducing a separate scoring formula.
- Focused regression coverage protects permissions, independent qualification decisions, reporting, responsive layouts, and notification behavior.

### Upgrade Considerations

Upgrading from v1.1.3 requires core migrations **0087 through 0090**, ending at `0090_reader_rejection_notices`. These cover independent branding modes, scheduled-report cleanup tracking, Host review access, and Reader rejection notices.

Take a fresh database, media, and configuration backup before deployment, and preserve the encryption material and a compatible rollback image. Apply migrations before serving the upgraded application. A rollback after schema changes requires the matching database backup as well as the previous image.

This release preserves existing scoring history and does not create notices for historical rejections. Review the new Host access and Discord controls through the administrative interface as appropriate. Repository publication does not deploy the release to a running installation.
