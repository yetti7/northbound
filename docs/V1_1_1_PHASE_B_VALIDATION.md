# v1.1.1 Phase B preparation

November 15, 2026. Production remains v1.1.0; no production changes, release tags, image publication or Discord activation are permitted by this phase.

## Candidate integration

Starting local/remote Dev: `8d0214c5fddb50cd11d3c3b445db0bf27b22c6d3`; remote main/v1.1.0: `a9e6d8896704db9a8e2b244a2c0f5fefc637438b`. Four production-only commits are merges; production has no content missing from Dev. Twenty Dev commits are absent from production before integration. Only unrelated untracked `scripts/` exists; it is excluded and retained.

Local branch `codex/v1.1.1-integration` starts at current production and merges Dev with `--no-ff`, producing `54c2f4de9b684cde5af435d48153199822d7d097`. There were no conflicts and its tree exactly matches Dev. Both source histories are ancestors. This Phase B documentation update follows that merge; the final candidate SHA is recorded in the PR/final report to avoid a self-referential commit hash.

Expected content includes migrations 0078–0082, completed Discord implementation, Issue #7, Issue #8, the Phase A Void regression and v1.1.1 release documents. No application changes, unexpected runtime files or workflow modifications are added in Phase B. A later approved main integration should retain this branch history with a normal merge; changes in main or candidate require renewed CI validation. Do not squash/rebase away the recorded integration evidence without review.

## Maintained CI and image assessment

The supported non-publishing route is a draft main-targeting PR from the integration branch. `container.yml` checks out GitHub's PR merge ref; confirm both parents and tree identity against the recorded candidate, because the synthetic PR merge SHA may differ. Results must be read from the actual run before claiming success. No dispatch on Dev is equivalent: the maintained validation jobs skip that dispatch. No workflow changes or redundant local full suite are needed. CI result is recorded in the final report; this document itself does not assert a pass.

The unchanged Dockerfile uses python:3.13-slim, installs requirements, copies the repository build context with `.dockerignore`, collects static files, creates `/data`, and runs as UID/GID 10001 with `./deploy/start.sh`. Use the PR checkout/build context, which excludes unrelated local untracked scripts and private `/tmp` rehearsal data. PR builds are linux/amd64 with push:false and no registry login/publication. A separate local build would duplicate that check and is not necessary.

After separate publication approval, main/tag workflows log in to GHCR using github.actor/GITHUB_TOKEN with packages:write, build linux/amd64 and linux/arm64, publish SHA/ref tags and promote latest on main/exact stable version tags. Tagged version is supplied through NORTHBOUND_VERSION. Eventual OCI revision/version, immutable index digest and both platform manifests remain unverified until publication; PR/local build success cannot establish them. Main merge triggers publication, so it requires separate authorization even without a release tag.

## Isolated recovery validation

Reused the protected Phase A production SQLite snapshot and v1.1.0 source; no new production data transfer, backup or restore occurred. Local disposable resources are under `/tmp/nb-v111-phase-a/phase-b/`, inside the existing mode-0700 directory. Socket connections are blocked by sitecustomize; all integration/worker flags are off and no background workers run. No original production secret/key was copied.

Using v1.1.0's real `create_stored_backup`, `validate_backup`, `stage_stored_restore` and `restore_pending_backup` paths: a local archive passed CRC and structural/database validation; its database was restored byte-for-byte from the archive member; a deliberately changed synthetic media sentinel was restored. An initial harness comparison incorrectly compared the SQLite backup API result to the source file bytes (SQLite headers can differ); it was corrected to compare the restored file with the actual archive member. Diagnostic evidence was retained, and no application fix was needed.

The compatible rollback schema remains core 0077. Migrate reported no pending upgrade under v1.1.0; SQLite integrity is ok and FK violations zero. Preserved representative counts: 807 submissions, 2 Groups and 3 Challenges. The mandatory forced Hardcover safeguard completed and quarantined 15 restored synchronization events; OAuth reconnect requirements were checked. This is the expected disposable restore safety mutation, not a production action or scoring reconciliation.

V1.1.0 WSGI initialized; health and login GETs returned HTTP 200. Password authentication/session creation passed using a new disposable account, without changing any real account password. Encryption/decryption round-trip passed using a synthetic local Django key and the existing fallback mechanism. Original production ciphertext decryption is not proven without the original keys. A missing collected-static directory warning is expected for source-only execution; container static collection is a separate CI image-build check.

Scope limits: this proves the application database/restore mechanism, synthetic media restoration and rollback-source startup/authentication. It is **not a complete installation recovery**. Actual production media bytes were not copied/restored; the existing private inventory proves all 42 unique stored references were available among 43 files at Phase A inspection, not that a paired media backup is recoverable. No production configuration/key archive or reverse-proxy maintenance procedure was available locally. Original production encryption keys, deployment environment/Compose recovery, exact rollback-image startup/static assets and full paired media recovery remain deployment-day verification requirements. Existing production ZIP structure was inspected read-only in the prior audit; this rehearsal uses a newly generated local-only archive from the protected snapshot, not a restored existing production ZIP.

## Startup and deployment readiness

The exact proposal remains to replace the obsolete command from `compose.tbr-title-2647958.json` with `command: ["./deploy/start.sh"]` while preserving other reviewed production settings, mounts, keys, networking and the pinned release image. Standard `set -eu` startup orders restore_pending_backup → migrate --noinput → safeguard_restored_hardcover_sync → existing backup/Challenge/Hardcover children → Gunicorn. Migrations/safeguards must succeed before any worker. Restart migration checks are idempotent; no parallel one-off migration or duplicate web replica is allowed.

Keep exactly one existing bundled Challenge scheduler and no dedicated Discord workers. Preserve existing backup/Hardcover settings. All four Discord global gates remain false, legacy delivery remains off and no Dev destinations are copied. External rollback uses the compatible v1.1.0 image/database, one-off migrations and forced restore safeguard before normal startup, without reintroducing the obsolete migration-skipping override. No live startup configuration was modified.

Use the ordered [deployment plan](V1_1_1_DEPLOYMENT_PLAN.md): explicit approval; maintenance ingress and write suspension; stop application writers; fresh paired DB/media backup; archive/DB/application recovery verification; protected original keys/config; verified immutable image; reviewed startup replacement; migrations; application start; health/access/scoring/media/scheduler/Discord-off checks; rollback decision; reopen writes. Restoring the snapshot after writes reopen loses newly accepted work. Discord activation is a separate subsequent procedure, using actual commands `run_discord_events --interval 30` and `run_discord_scheduler --interval 30`, destination/audience review, preferences/opt-ins, queue/heartbeat/delivery verification and worker-first emergency stop.

## Temporary artifacts and cleanup recommendation

No artifacts have been deleted. Keep the protected directory until recovery/release review is complete:

- Sensitive production data: `production-copy.sqlite3`, `migrated.sqlite3`, their possible SQLite sidecars, Phase B disposable recovery databases, pre-restore copies and local backup ZIPs. These contain real accounts, reading records and encrypted credentials; restored copies also contain expected safeguard/auth-test changes.
- Sensitive metadata: `media-private.json` contains filenames/hashes; `before-scores-private.json`, `after-scores-private.json` contain record identifiers/totals; `baseline-private.json` contains schema/count/digest metadata; `runtime.json` contains sanitized operational configuration. Retain privately for evidence review; do not commit or attach their contents.
- Non-production source/support: extracted `v110/`, local harness scripts, sitecustomize, aggregate reports and logs. Review logs as potentially sensitive before sharing. Phase B recovery logs include only aggregate outcomes and private temporary paths.

After explicit cleanup approval, inventory only this known release directory (including nested backup/pre-restore and sidecar files), retain required aggregate evidence outside it, then remove the approved exact paths. Normal deletion is not a guarantee of physical erasure on APFS/SSD/backups; encrypted storage and retention management govern residual copies. Do not clean unrelated `/tmp` directories or prune Docker resources. No production recovery point should depend on these disposable rehearsal artifacts.

## Remaining gates

Final recommendation is **READY WITH REMAINING GATES** until the actual maintained CI result is confirmed, publication produces a verified immutable multi-platform artifact, complete paired recovery/configuration/key verification succeeds, and explicit release/deployment approval is granted. The obsolete startup override remains unchanged and must be replaced only in the approved deployment window. Separate Discord activation and any deferred Delivery Status UI improvement are outside this release preparation.
