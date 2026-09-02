# Project overview

A concise architecture reference, not installation instructions or a release history.
Start with the [README](../README.md); use [Self-hosting](SELF_HOSTING.md) for deployment.

## Product and data boundaries

Northbound coordinates reading Groups and their Challenges. V1 reading history is
generated through Challenge participation and approved submissions, not a standalone
personal diary or imported provider history.

- Accounts and reusable Reader profile data are installation-wide; Group membership,
  Challenge registration, staffing and team assignment are separate relationships.
- Group Owner/Moderator/Member authority is separate from Challenge Hosts, Team
  Leaders and Floaters. Platform Administration does not create participation.
- Original submission values, approved base pages and reward adjustments are distinct.
  Themes, BOTM, Personal TBR, Manual Rewards / Games and checkpoints contribute through
  their defined workflows rather than arbitrary score edits.
- Team/participant reports use the appropriate scoring or historical planning data
  for their purpose; visibility depends on the configured authority and privacy rules.
- Audit and recovery records preserve operational history. There is no generic
  credential fallback or Reader impersonation path for Hardcover synchronization.

## Runtime

A Django application serves browser pages and uploaded media. SQLite with persistent
local storage is the normal single-container installation; PostgreSQL is optional.
Container startup restores staged SQLite backups, migrates, safeguards restored sync,
then starts the web server, backup/Challenge schedulers and Hardcover worker.

Database records hold queued Reader sync work, provider mappings and attempt history.
The worker is bounded and consent-aware; its local lock does not support distributed
application replicas. External provider availability is separate from local approval,
scoring and database health.

See [Hardcover](HARDCOVER.md) for credential ownership and forward-only synchronization,
and [Backup and restore](BACKUP_RESTORE.md) for why restored external work is quarantined.
