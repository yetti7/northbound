# Roles and monthly lifecycle

Brief terminology reference, not a detailed role guide. For installation and
operations start with [Self-hosting](SELF_HOSTING.md).

## Roles

| Role | Scope | Main capabilities |
|---|---|---|
| Platform owner | Entire installation | Unrestricted installation administration, including Challenge operations and review when needed, without implicit Group membership, staffing, participation, or scoring |
| Group owner | One group | Group administration, permission management and Challenge TBR assistance for participants in their Group; Challenge operation follows the applicable authority rules |
| Moderator | One group | Delegated group authority; current defaults cover Group announcements, Challenge TBR assistance within their Group and restricted team statistics, with per-member capability overrides; Challenge review requires staffing |
| Member | One group | Persistent group membership; Reader participation is determined separately for each challenge month |
| Host | One challenge month | Operates existing Challenge teams, rosters, enrollment administration, themes, Challenge announcements, submission soft-removal, and Challenge-wide submission/theme-claim review; manages participant Challenge TBRs in that Challenge; assignment itself does not create participation |
| Team Leader | One challenge month and team | Competing Reader who is already enrolled and assigned to that team; reviews submissions and theme claims and manages Challenge TBRs only for Readers currently on that team |
| Floater | One challenge month | Non-competing, no-team staffing support with Challenge-wide submission and theme-claim review and Challenge TBR assistance; no Host, team-management, scoring, visibility, or completed-book submission authority |

An account can belong to multiple groups with a different role in each. Creating a group makes a normal user its owner. Joining with an access code creates a member record; a group owner or Platform Owner can then adjust that role.

Platform owners have equal installation-wide authority and remain separate from group roles. Code deployment and schema migrations remain deployment operations.

TBR assistance does not grant registration-answer, account-setting or credential
access. Staff may save drafts, complete nine valid selections without completing
registration, and make protected post-lock corrections. See the
[staff TBR workflow](HOST_GUIDE.md#help-a-reader-manage-their-challenge-tbr).

## Challenge-month states

| State | Meaning |
|---|---|
| Draft | Challenge preparation |
| Upcoming | Published/pre-start Challenge; registration follows its separate gate |
| Active | Active Challenge participation and submission period |
| Finalizing | Submissions have ended; authorized staff finish review |
| Completed | Completed Challenge history; reopening requires explicit recovery confirmation |
| Archived | Historical read-only view |

Registration being open is separate from lifecycle state. Normal lifecycle changes
move one adjacent stage at a time; moving backward requires explicit confirmation.
Archived Challenges cannot move backward through the normal lifecycle.
