# Roles and monthly lifecycle

## Roles

| Role | Scope | Main capabilities |
|---|---|---|
| Platform owner | Entire installation | Unrestricted installation administration, including Challenge operations and review when needed, without implicit Group membership, staffing, participation, or scoring |
| Group owner | One group | Full group control, permission management, exports, retention decisions |
| Moderator | One group | Delegated group authority; current defaults cover Group announcements and restricted team statistics, with per-member capability overrides; Challenge review requires staffing |
| Member | One group | Persistent group membership; Reader participation is determined separately for each challenge month |
| Host | One challenge month | Operates existing Challenge teams, rosters, enrollment administration, themes, Challenge announcements, submission soft-removal, and Challenge-wide submission/theme-claim review; assignment itself does not create participation |
| Team Leader | One challenge month and team | Competing Reader who is already enrolled and assigned to that team; reviews submissions and theme claims only for Readers on that team |
| Floater | One challenge month | Non-competing, no-team staffing support with Challenge-wide submission and theme-claim review; no Host, team-management, scoring, visibility, or book-entry authority |

An account can belong to multiple groups with a different role in each. Creating a group makes a normal user its owner. Joining with an access code creates a member record; a group owner or Platform Owner can then adjust that role.

Platform owners have equal installation-wide authority and remain separate from group roles. Code deployment and schema migrations remain deployment operations.

## Challenge-month states

| State | Meaning |
|---|---|
| Draft | Staff configure dates, teams, rules, and visuals |
| Open | Readers can submit completions |
| Closed | New submissions stop while moderators finish reviews |
| Finalized | Scores and standings are locked |
| Archived | Historical read-only view |

Reopening a finalized month will require a reason and audit event.
