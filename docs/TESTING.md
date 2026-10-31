# Automated validation

Maintained automated test **source** belongs in Git. Databases, caches, coverage,
reports, credentials, media, and backups do not. Tests are excluded from production
Docker build contexts by `.dockerignore`; normal application modules and migrations
remain included.

## Reviewed source inventory for v1.0.2

The previously ignored Python sources were inspected for test classes/methods,
local imports, fixture dependencies, and current behavioral contracts. Historical
migration tests and older phase/foundation tests remain maintained: they verify
backward compatibility and authority boundaries, not obsolete application versions.
No temporary debugging scripts or generated artifacts were found among those modules.

| Classification | Disposition |
| --- | --- |
| 77 maintained Django test modules across `core` and `reading_challenge` | Versioned and included in the maintained suite |
| `core/test_recovery_fixtures.py` | Versioned test-only protected dependency graph; replaces the recovery-domain tests' demo-seeder dependency |
| `static/js/personal_tbr_registration_helpers.test.js` | Versioned, dependency-free JavaScript assertions |
| `core/test_demo_data.py` | Active development-demo validation; remains local-only with its excluded generator/command |
| Obsolete/superseded tests | None identified for removal; no ignored files were deleted |
| Debugging scripts/generated artifacts | None reclassified as maintained test source |

The review retained 75 of the previously ignored 76 Python modules. The remaining
module tests the intentionally excluded development demo generator. The existing
tracked fresh-install module and the new percentage module complete the 77-module
maintained suite. Local discovery also runs the demo tests when they are present;
a clean source checkout has no dependency on that generator.

## Run the suite safely

Use installed project dependencies and a disposable SQLite location, never the
application or production database:

```sh
NORTHBOUND_SQLITE_PATH=/tmp/northbound-test-base.sqlite3 \
  python manage.py test core reading_challenge --noinput --verbosity 1
node static/js/personal_tbr_registration_helpers.test.js
```

Django creates and destroys its isolated test database. Migration tests deliberately
move that **test** database between historical schemas. They do not authorize
migration of the application database. External Hardcover calls are mocked by tests.

Focused percentage and scoring validation:

```sh
NORTHBOUND_SQLITE_PATH=/tmp/northbound-test-base.sqlite3 \
  python manage.py test core.tests_theme_percentage \
  core.test_theme_review_integration core.test_recovery_scoring \
  core.test_score_aggregation core.test_progress_checkpoints --noinput
```

The maintained suite takes approximately seven minutes in the current local
Python 3.13 environment. CI runs the entire maintained Django suite; coverage is
not reduced to the focused command. PostgreSQL and SQLite migration checks remain
separate required jobs/steps. The container publishing job depends on successful
Django tests and PostgreSQL checks, then performs the existing SQLite/Compose checks.
Tag selection and amd64/arm64 publication are unchanged. No image is published by
running these local commands.

## Percentage regression contracts

- Whole percentages 0–100, default 0; Approved Pages only; exact per-claim half-up rounding.
- Independent stacking and canonical page-valued provenance; no duplicate counting.
- Permanent rate lock after any claim, even after claim purge.
- Finalized zero awards survive ordinary review and recovery.
- Intentional page corrections update established rejected-award snapshots while
  keeping claims rejected and their provenance inactive; reapproval uses the correction.
- Authorized eligibility restoration establishes missing first-time awards while
  preserving existing awards, including legacy fixed-page behavior.
- Corrections/restoration are atomic; historical checkpoint snapshots are unchanged.

Browser smoke testing, actual container execution, and a representative PostgreSQL
upgrade require separate environment validation. Fresh-database migration checks
alone do not prove those behaviors.
