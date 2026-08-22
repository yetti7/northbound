# Roles and monthly lifecycle

## Roles

| Role | Scope | Main capabilities |
|---|---|---|
| Platform super-administrator | Entire installation | Provision groups, support owners, inspect audit history, manage integrations and recovery |
| Group owner | One group | Full group control, administrator management, exports, retention decisions |
| Group administrator | One group | Months, participants, teams, themes, challenges, reports |
| Moderator | One group | Review submissions, approve claims, resolve duplicates, record adjustments |
| Game manager | One group | Upload game artwork and update permitted visual tracker state |
| Reader | One group | Submit books and view personal/team progress |

An account can belong to multiple groups with a different role in each. Creating a group makes a normal user its owner. Joining with an access code creates a reader membership; an owner or platform root can then adjust that role.

The platform super-administrator is a support role, not a web-based development shell. Code deployment and schema migrations remain deployment operations.

## Challenge-month states

| State | Meaning |
|---|---|
| Draft | Staff configure dates, teams, rules, and visuals |
| Open | Readers can submit completions |
| Closed | New submissions stop while moderators finish reviews |
| Finalized | Scores and standings are locked |
| Archived | Historical read-only view |

Reopening a finalized month will require a reason and audit event.
