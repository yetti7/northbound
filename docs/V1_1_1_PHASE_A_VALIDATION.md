# v1.1.1 Phase A validation

## Verified baseline

November 15, 2026: local/remote Dev runtime candidate `8fa022f96b15fd2cb1a1bf67705015a27c5c19d8`; remote main and dereferenced v1.1.0 tag `a9e6d8896704db9a8e2b244a2c0f5fefc637438b`. Local main remains stale, deliberately unchanged. Merge base `f5c22412df0343556b0e8f68fa2daf48f438e38d`; production-side four commits are merges with no net production content missing from Dev. Dev adds 18 commits, 141 files, 8,331 insertions/333 deletions. Initial working tree contained only unrelated untracked `scripts/`, preserved.

Read-only runtime reinspection confirms production still runs v1.1.0 revision a9e6d889 with Docker health healthy, its original image and startup override, one bundled Challenge scheduler and no dedicated Discord workers. No production mutation or messaging occurred.

## Submission validation

New Phase A run: **109 focused existing tests passed** in 42.679 seconds, system checks clean, using an isolated local SQLite test database and Python socket connections blocked. Modules: submission Void, restore, corrections, association, delegated permissions, unified review, workspace and Discord correction transactions.

Coverage includes approval, signed metadata/page corrections, authorized BOTM/TBR credit changes, approved Review scoring/status guards, audit preservation, frozen Theme/completion evidence, Immersive correction behavior, stale previews, transactional rollback, Void credit removal and checkpoint/history preservation. The accepted workflow provides corrections and Void for Approved records, not reversal to Pending/Rejected. No permission or application workflow changes are included in Phase A.

Additional validation: 14 existing scoped-review/Theme tests passed. One new focused regression passes: Void after a signed page correction with approved Immersive credit removes Reader and Team totals while retaining correction/removal audits and approved Immersive history. Its first run exposed a test-fixture mistake (TestCase held an outer transaction); the standalone regression now uses TransactionTestCase so the correction service owns its transaction. No application fix was needed. Total distinct passing tests: **124**. Historical targeted passes are context only, not final-candidate CI.

## CI and artifacts

One read-only Actions query found **zero runs for original candidate 8fa022f**. Final documentation/test commit requires maintained PR CI against current main. No CI dispatch, PR merge, release tag, release publication or image publication occurred. Immutable v1.1.1 image/platform verification remains pending publication authorization and successful build. Do not claim full candidate validation from focused tests.

## Migration rehearsal and recovery

The user explicitly approved the sensitive SQLite transfer to `/tmp/nb-v111-phase-a/production-copy.sqlite3` after automatic review required destination-specific approval. The source was opened mode=ro with query_only; SQLite backup copied into memory and streamed locally, without a production file or backup write. The local directory is mode 0700 and database copies mode 0600, outside Git. No production keys were copied: synthetic local keys suffice because this rehearsal never decrypts credentials or invokes integrations.

**PASS:** migrations 0078–0082, 77→82, completed in **0.846 seconds** on the disposable copy. SQLite integrity is ok and foreign-key violations are zero before/after. All 60 original application tables retain every original column value and row, including BOTM/TBR evidence, approved scoring receipts, Team assignments and media references. Only migration history, content types and permissions changed, as expected from migrations/post_migrate. Five Discord tables were created. Counts preserved: 807 submissions, 2 Groups, 3 Challenges and 51 Team assignments. This fresh snapshot differs from the older audit because production remains in use.

All authoritative Reader and Team totals calculated under v1.1.0 before migration and current candidate after migration are identical. WSGI initialized successfully, health and login responses returned HTTP 200 through Django's in-process client against the migrated database with query_only enabled. No schedulers/background workers ran; Python network connections were blocked throughout application execution. Discord global gates, legacy delivery, destinations, report schedules, event queue and per-Challenge opt-ins were verified off/empty. Migration output contained no warnings or schema failures; Django migration detection reported no changes. An initial startup assertion referenced the wrong model field; correcting the harness to inspect report-schedule preferences resolved it, without database or application repair.

Physical media verification: **PASS**. After separate explicit user approval, a read-only inventory of production media filenames and SHA-256 hashes was transferred to `/tmp/nb-v111-phase-a/media-private.json` (mode 0600 inside the protected directory). It contains 43 files; all 42 unique stored media references resolve to inventoried files, with zero missing references. Reference values are identical before/after migration. No filenames, personal data or media bytes are included in this report or Git. This inventory was collected after the database snapshot while production remained active; it establishes file availability at inspection, not an atomic paired recovery point or a pre/post production hash comparison.

Physical media files were not copied or restored. Full application/media restore remains unverified. Candidate migration startup is not a complete recovery test, and archive readability alone would not establish application restoration. Fresh deployment-day paired backup, protected configuration/keys and off-host recovery remain required.

## Remaining gates

- Complete: baseline, release-note draft, accepted workflow documented, focused submission validation and production-copy migration rehearsal, physical media reference verification, startup/recovery/deployment and separate Discord activation plans.
- Pending authorization: later merge/publication and production deployment/maintenance operations; future Discord activation.
- Pending validation: complete application/media restore; final-candidate maintained CI; published immutable multi-platform image verification; deployment-day fresh paired backup, protected key/configuration recovery and off-host verification; exact proxy maintenance action.
- Deployment blocked until remaining required validation and recovery gates pass. The obsolete override is a documented deployment blocker, intentionally unchanged in Phase A.

Recommendation: **READY WITH REMAINING GATES**. Production remains on v1.1.0; no merge, tag, publication, deployment or Discord activation is authorized by this report.
