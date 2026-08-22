# Project overview

## Product direction

Northbound is a durable replacement for monthly Google Forms and formula-heavy team spreadsheets. It keeps verified reading activity separate from game bonuses so historical reading totals remain trustworthy.

## Macro domains

1. **Platform operations** — super-administrator support, group provisioning, audit history, backups, and upgrades.
2. **Groups and people** — group owners, administrators, moderators, game managers, readers, and historical membership.
3. **Monthly lifecycle** — draft, open, closed, finalized, and archived challenge months.
4. **Reading records** — book/edition selection, submitted pages, metadata pages, approved pages, and review decisions.
5. **Challenges** — TBR, BOTM, prompt lists, date-based themes, and manual claims.
6. **Visual trackers** — browser-rendered boss progress, team races, and uploaded manual-game boards.
7. **Reporting** — personal progress, team totals, Reader Rumble, audit reports, and spreadsheet exports.

## Source-of-truth rules

- PostgreSQL is the long-term source of truth.
- A submission's original values are retained after review.
- Verified reading pages are not modified by bonuses, deductions, steals, or multipliers.
- Monthly team membership never overwrites historical team membership.
- Finalized months are read-only unless an authorized user reopens them with a recorded reason.
- Support impersonation and exceptional data repairs must be conspicuous and audited.
- Publicly registered accounts may create a group or join one with its private access code.
- Groups receive a unique six-character access code that owners can regenerate; successful joins always begin with the reader role.

## First usable milestone

1. A platform operator completes first-run setup.
2. A group owner creates an August challenge month.
3. Readers are assigned to teams for that month.
4. A reader submits a completed book.
5. A moderator approves or corrects its page count.
6. Personal and team totals update from approved submissions.
7. The month can later be finalized and exported.

## Deliberate non-goals for milestone one

- generalized game-rule engine;
- automatic Amazon integration;
- live Google Sheets synchronization;
- arbitrary code or SQL execution through the super-admin interface;
- native mobile applications.
