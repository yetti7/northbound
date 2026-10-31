# Release notes

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
