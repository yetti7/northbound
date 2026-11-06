# v1.0.9 final release review and execution plan

Status: **READY for the authorized release workflow**, subject to its normal CI,
image-publication, fresh production preflight and verified-backup gates. This
package is prepared locally; nothing has been committed, pushed, merged, tagged,
published or deployed by this review. Production was not accessed.

## Reviewed scope and exclusions

Compare the complete working tree (including new files) to local tag `v1.0.8`
(`9df7643`). The existing dev commits `12adcd7`, `1c4a14f` and `f80085f` add the
previously approved isolated-development workflow/runtime safeguards, correction
UX and access documentation. They are intentional parts of the PR delta, not new
CI changes made during this review.

The remaining release includes challenge visibility, focused submission management,
immutable Reader intent, conservative work/edition matching, Reader cards, unified
scoped review, and administrative credit assignment/reversal/restoration. Matching
and reward services remain authoritative. Migration 0065 only adds neutral-default
fields and a suggested-method choice. No historical data backfill, score repair,
provider queue generation or credential migration is included.

Exclude these unrelated, untracked recovery artifacts from staging and packaging:

- `scripts/SUBMISSION_72_RECOVERY.md`
- `scripts/recover_submission_72.py`
- `scripts/rehearse_submission_72.py`

Preserve them locally. Never use `git add .`, `git add -A`, or `git clean`. Do not
stage local databases, backups, `.env`, keys, temporary acceptance reports or
synthetic fixtures. `.env.example` and `.env.dev.example` contain placeholders;
the examples are not production secrets. The working-tree release allowlist is
recorded in `V1_0_9_RELEASE_FILES.txt`; inspect it again immediately before staging.

## Validation evidence

- Final consolidated run: **251 targeted Django cases**, 250 passing initially.
  The only failure was an older percentage-Theme POST fixture missing the required
  Phase 2C `matching_version`; the fixture now GETs the form first. Its focused
  rerun passed, including the 500-base/125-Theme/625-total and approval-audit checks.
  No application change was required.
- The run includes `WorkEquivalenceTests.test_abridgement_uncertainty_is_reviewable_not_automatic`,
  the earlier history-locked BOTM fixture regression. It passes without editing a
  persisted entry after match history exists.
- Reader intent, work/format equivalence, both matching domains, durable decisions,
  duplicate prevention, scope/lifecycle, unified approval, administrative previews,
  reversal/restoration, corrections, historical compatibility, visibility,
  percentage/fixed awards, completion, audit and default/development runtime checks
  are included. The full repository suite was not run.
- Nine changed/new JavaScript files passed syntax checking; four focused JavaScript
  test programs passed. `deploy/start.sh` passed shell syntax checking.
- Migration drift check: none. Fresh isolated SQLite migrations through 0065 pass;
  the targeted 0064-to-0065 historical-preservation test also passes. PostgreSQL
  remains a mandatory established CI gate, not a claimed local validation result.
- Stage 2 acceptance used deployed endpoints and an actual synthetic
  Reader/Floater/Host browser chain. All 3,402 original records across 55 tables,
  including 274 community submissions, were unchanged. No sync outbox entries were
  created. Synthetic data stays only in isolated Dev and is not a release input.

Five missing-CSRF POST probes returned 403 after initializing the isolated test
installation. Security review of changed paths found existing CSRF middleware/tokens in place,
Reader-owned and Challenge-scoped choices, signed actor/state-bound previews,
current reviewer scope checks, unique qualification constraints, atomic rollback,
and preserved historical evidence. Detailed submission audit history remains
Platform Administrator-only. Group Owners/Hosts have credit-management scope;
Floaters/Team Leaders have supported-suggestion confirmation, not arbitrary override.
Administrative preservation checks reject unrelated mutations and changes to frozen
completion qualification/evidence.

## Accepted limitations

Copied Dev Hardcover credentials do not decrypt under Dev's encryption key. Live
catalog validation could not proceed; cached/simulated integration and the matching
paths passed. This is accepted, not a new application defect. Do not recover keys,
copy production secrets, reconnect OAuth or change Dev credentials in this release
review. Include a read-only live search in the separately authorized production
smoke test. Different-work translations and changes affecting frozen completion
awards still require separately reviewed resolution. No deferred features are added.

## Proposed commits and PR

After explicit publication authorization, while remaining on `dev`:

1. `feat: finalize v1.0.9 submission management and challenge credit`
   - All reviewed implementation, migration, templates, scripts and focused tests,
     including the review-snapshot fixture correction. Keep interconnected model,
     service and UI changes together so the commit remains runnable.
2. `docs: prepare v1.0.9 release and upgrade guidance`
   - Release notes, self-hosting considerations, this review/runbook and allowlist.

PR title: **Northbound v1.0.9: submission management and reliable BOTM/TBR credit**

PR description:

> Readers can miss challenge credit when configured titles differ from catalog
> titles, and reviewers previously needed separate correction/matching screens.
> Add persistent Reader selection intent, conservative shared-work matching and
> unified approval with explicit independent BOTM/TBR confirmations. Add scoped
> administrative assignment/reversal/restoration with reasons, signed scoring
> previews and preserved audit/award history.
>
> Simplify Manage Submission and correction previews, retain Hardcover correction
> safeguards, hide pre-completion aggregate card statistics from non-superusers,
> and expose Completed/Archived competitive results to active Group members while
> retaining discovery and private-information boundaries.
>
> Migration: additive core.0065; no historical backfill or score/sync recalculation.
> Validation: 251 focused Django cases accounted for (one legacy fixture corrected
> and retested), nine JS syntax checks, four JS test programs, migration consistency
> and isolated migration/historical compatibility checks, plus synthetic Dev
> endpoint/browser acceptance. Live Dev Hardcover testing is an accepted encryption-
> key limitation; include a production read-only catalog smoke test. Existing
> development-image workflow/runtime safeguards are included from prior dev work.
>
> Excludes one-time recovery scripts, synthetic fixtures, secrets and deferred work.
> Production deployment requires separate approval and a verified backup.

## Publication procedure — NOT EXECUTED

Use the repository's existing GitHub workflows. Publication and production
application deployment are separate authorizations. An authorized push to `dev`
can publish development images; a merge to `main` can publish `latest` even before
creating the stable tag. Do not run these commands under review-only authorization.

```sh
git branch --show-current                 # must be dev
git status --short
git diff --cached --stat                  # inspect exact staged set, never add all
python3 - <<'PYFILES'
from pathlib import Path
paths = Path('docs/V1_0_9_RELEASE_FILES.txt').read_text().splitlines()
assert not any(p.startswith('scripts/') for p in paths)
Path('/tmp/northbound-v109-code-paths.txt').write_text('\n'.join(p for p in paths if not p.startswith('docs/')) + '\n')
Path('/tmp/northbound-v109-doc-paths.txt').write_text('\n'.join(p for p in paths if p.startswith('docs/')) + '\n')
PYFILES
git add --pathspec-from-file=/tmp/northbound-v109-code-paths.txt
git diff --cached --check
git diff --cached                        # stop unless this is exactly reviewed code/tests
git commit -m 'feat: finalize v1.0.9 submission management and challenge credit'
git add --pathspec-from-file=/tmp/northbound-v109-doc-paths.txt
git diff --cached --check
git diff --cached                        # stop unless this is exactly reviewed documentation
git commit -m 'docs: prepare v1.0.9 release and upgrade guidance'
git fetch origin --prune
git log --left-right --oneline origin/dev...dev
# Stop on unexpected remote divergence; do not force-push or merge unseen changes.
git push origin dev
gh pr create --base main --head dev --title 'Northbound v1.0.9: submission management and reliable BOTM/TBR credit' --body-file /tmp/northbound-v109-pr.md
# /tmp/northbound-v109-pr.md must contain the reviewed PR description above.
```

Record the PR number and reviewed head SHA. Check required PR jobs with
`gh pr checks "$pr_number" --required`. If pending, report pending and wait for
completion; do not bypass checks or repeatedly poll. Require the established
Django, PostgreSQL/migration and container jobs to succeed. Review all new diff
content if the head changes.

```sh
reviewed_head=$(git rev-parse dev)
gh pr merge "$pr_number" --merge --match-head-commit "$reviewed_head"
git fetch origin main
merge_sha=$(gh pr view "$pr_number" --json mergeCommit --jq '.mergeCommit.oid')
test "$(git rev-parse origin/main)" = "$merge_sha"
# Require the existing main workflow checks to finish successfully on merge_sha.
# Stop if main advanced; do not tag an unreviewed commit.
git tag -a v1.0.9 "$merge_sha" -m 'Northbound v1.0.9'
git push origin refs/tags/v1.0.9
```

Extract the v1.0.9 body of `docs/RELEASE_NOTES.md` to a temporary notes file, without
historical sections. Inspect it, then publish using the established release style:

```sh
python3 - <<'PY'
from pathlib import Path
text = Path('docs/RELEASE_NOTES.md').read_text()
body = text.split('\n## v1.0.9\n', 1)[1].split('\n## v1.0.8', 1)[0]
Path('/tmp/northbound-v109-release-notes.md').write_text(body.strip() + '\n')
PY
gh release create v1.0.9 --verify-tag --title 'Northbound v1.0.9' --notes-file /tmp/northbound-v109-release-notes.md --latest
docker buildx imagetools inspect ghcr.io/yetti7/northbound:v1.0.9
docker buildx imagetools inspect ghcr.io/yetti7/northbound:latest
```

Check the tag workflow and image availability once; if pending, report pending and
stop. A published release is not proof the image exists. Before deployment require
both linux/amd64 and linux/arm64 descriptors, successful tag CI, revision equal to
`merge_sha`, version `v1.0.9`, and matching stable/latest index digests. Record the
immutable release digest. Do not deploy an unverified tag or silently substitute a
main/development image.

## Production deployment — separate explicit authorization required

No production inspection was performed for this review. This procedure uses the
existing Compose/startup and backup/restore services. Fresh preflight is mandatory;
never infer current runtime paths or state from old notes.

On the authorized production host, identify only container `northbound`. Verify
Compose project `northbound`, service `web`, expected production URL/port (8060),
SQLite `/data/northbound.sqlite3` and mount `/srv/appdata/northbound:/data`. Derive the
exact project directory and ordered config file list from its Compose labels; stop
on unexpected services, database engine, paths or pending restores. Do not print
resolved environment values. Preserve the original keys/configuration and record
all image/container IDs and Compose checksums privately. Use a quiet maintenance
window; do not allow writes between the recovery backup and successful smoke tests.
Confirm no provider write operation is in-flight before stopping processes.

The following Bash array uses only the validated production project/configuration:

```bash
prod_dir=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' northbound)
prod_files_csv=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' northbound)
IFS=',' read -r -a prod_files <<< "$prod_files_csv"
prod_compose=(docker compose -p northbound --project-directory "$prod_dir")
for prod_file in "${prod_files[@]}"; do prod_compose+=(-f "$prod_file"); done
"${prod_compose[@]}" config --quiet
old_image_id=$(docker inspect --format '{{.Image}}' northbound)
old_image_ref=$(docker inspect --format '{{.Config.Image}}' northbound)
test "$(docker image inspect --format '{{.Id}}' "$old_image_ref")" = "$old_image_id"
# Stop on a mismatch. A pre-upgrade backup must run the verified old image.
"${prod_compose[@]}" stop web
```

With all production writers stopped, create a database-plus-media backup using the
existing v1.0.8 service. This overrides startup only for the backup process, so it
does not migrate, restore or launch workers:

```bash
"${prod_compose[@]}" run --rm -T --no-deps --pull never --entrypoint python web - <<'PY'
import os, json, hashlib, zipfile
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'reading_challenge.settings')
import django
django.setup()
from core.backups import create_stored_backup, validate_backup
os.umask(0o077)
path = create_stored_backup(automatic=False)
path.chmod(0o600)
metadata = validate_backup(path)  # includes extracted SQLite integrity_check
with zipfile.ZipFile(path) as archive:
    assert archive.testzip() is None
    media_count = sum(name.startswith('media/') and not name.endswith('/') for name in archive.namelist())
print(json.dumps({'backup': str(path), 'integrity': 'ok', 'media_files': media_count,
                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}))
PY
```

Record the actual returned filename. Copy the verified ZIP to protected off-host
storage; compare SHA256 and ZIP/SQLite validation there. Securely preserve `.env`,
original keys, exact Compose configuration/checksums and the old immutable image
reference separately, outside source control and media. **Stop if any backup or
verification fails.** Do not reset data, restore a Dev snapshot or run recovery #72.

Only after backup verification, pull the existing production service image. With
the standard moving `latest` configuration, verify that its RepoDigest equals the
approved stable v1.0.9 digest before continuing. If the installation pins an old
version, stop and explicitly select the approved digest in its existing deployment
configuration; do not substitute an unreviewed override or retag a shared image.

```bash
"${prod_compose[@]}" pull web
# Verify selected image RepoDigest, architecture, version and revision against
# the approved v1.0.9 manifest. Stop if latest moved or selection differs.
"${prod_compose[@]}" run --rm -T --no-deps --pull never --entrypoint python web manage.py migrate --plan
# Must show only the expected core.0065 upgrade from the verified v1.0.8 schema.
"${prod_compose[@]}" up -d --no-deps --pull never web
"${prod_compose[@]}" ps web
docker exec northbound python manage.py migrate --check
docker exec northbound python manage.py makemigrations --check --dry-run
```

Normal production startup retains restore handling, migrations, restored-sync
safeguards and existing scheduler/write-back defaults. Do not use Dev's direct-
Gunicorn override, cookie settings, disabled-worker environment or banner. Do not
reconfigure keys or OAuth. Do not recreate other services or use `down -v`/prune.

## Postdeployment smoke checklist

Before reopening normal activity:

- Health HTTP 200, login, expected image version/revision, migrations through 0065,
  no drift, expected Gunicorn workers and no unexpected startup errors.
- Expected Groups, challenge lifecycle and media present; compare original row
  values using equivalent raw SQLite representations (avoid int/bool converter
  mismatches). New intent fields remain neutral for historical submissions.
- Active card totals hidden for ordinary users; administrator oversight remains.
  Completed/Archived standings require active Group membership; private review and
  history controls retain their separate permissions.
- Submit Book picker, manual/catalog choices, review queue, unified approval,
  Manage Submission, administrative credit page and administrator history render.
  Do not submit a production test book or apply a production correction.
- One read-only live Hardcover search for a known book, edition list and selection.
  The ordinary Reader pathway writes catalog/cache rows: for a strictly read-only
  smoke check use a process-local in-memory DB clone with live read-only transport,
  no durable cache/catalog/session writes and no GraphQL mutations. Stop on provider,
  credential, rate-limit or incomplete-data failures; no key recovery or reconnect.
- No migration/correction-induced historical sync enqueue or award recalculation.
  Distinguish any independently evidenced normal activity from upgrade effects.
- Backup retained, rollback image available, production configuration/keys unchanged,
  development environment unchanged. Record results and only then resume normal use.

## Rollback — never automatic; fresh approval and write quiescence required

An image-only downgrade is unsafe for new decision semantics. Do not reverse 0065
or delete intent/evidence in place. If a rollback is considered, stop `web` and all
writers first. Make/verify a separate failed-upgrade database/media backup with the
currently selected image. If new activity has occurred, **do not overwrite it with
the older snapshot**: preserve it and request a separately reviewed compensating
recovery/data-loss decision. Provider mutations cannot be undone by a DB restore.

Only when authorized to restore the pre-upgrade recovery point:

1. Select the recorded old immutable image in a removable Compose override for
   **web only**, retaining the original production environment/mounts/command.
   Validate effective Compose; keep the regular service stopped. Do not retag
   `latest`, touch Dev or delete any data volume.
2. Under that compatible old image, stage the verified pre-upgrade native ZIP
   through `core.backups.stage_stored_restore(path)`. Run the existing
   `manage.py restore_pending_backup` explicitly in a one-off process, followed by
   `manage.py migrate --noinput` and the mandatory
   `manage.py safeguard_restored_hardcover_sync --force` before starting workers.
3. Use the canonical exact command pattern, with `rollback_compose` containing the
   same validated config files plus the authorized old-image-only override:

   ```bash
   # Set pre_upgrade_zip to the verified /data/backups/... path; validate its SHA256.
   "${rollback_compose[@]}" run --rm -T --no-deps --pull never --entrypoint python web - "$pre_upgrade_zip" <<'PY'
   import os, sys
   os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'reading_challenge.settings')
   import django
   django.setup()
   from core.backups import stage_stored_restore
   stage_stored_restore(sys.argv[1])
   PY
   "${rollback_compose[@]}" run --rm -T --no-deps --pull never --entrypoint python web manage.py restore_pending_backup
   "${rollback_compose[@]}" run --rm -T --no-deps --pull never --entrypoint python web manage.py migrate --noinput
   "${rollback_compose[@]}" run --rm -T --no-deps --pull never --entrypoint python web manage.py safeguard_restored_hardcover_sync --force
   "${rollback_compose[@]}" up -d --no-deps --pull never web
   ```

4. Stop if any restore, migration or safeguard fails. Verify old health/version,
   restored database/media and quarantined sync status. A restore may require
   personal OAuth reconnection; do not bypass quarantine or replay historic writes.
   Preserve pre-upgrade and failed-upgrade backups and all operator evidence.

See [Backup and restore](BACKUP_RESTORE.md) for the authoritative native ZIP and
external-restore safeguards. No restore or deployment command in this document has
been executed during final release review.
