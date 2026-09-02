# Release notes

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
