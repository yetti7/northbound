# Canonical Demo Data

Northbound includes a deterministic development/demo dataset for local manual testing and visual demonstrations. It is development tooling, not an application feature or production setup path.

The command refuses to run unless Django debug mode is enabled:

```bash
DJANGO_DEBUG=1 python manage.py seed_demo_data
```

Rerunning the command verifies the existing marked dataset without duplicating it. To remove only the marked demo-owned records and recreate the canonical dataset:

```bash
DJANGO_DEBUG=1 python manage.py seed_demo_data --reset
```

Do not run either command on DeepNorth or another production installation. `--reset` does not clear the database: it requires Northbound's private demo ownership markers and preserves unrelated records, all Platform Owners, Platform Owner credentials, invitations, and Platform Owner audit history.

All demo accounts use this development-only password:

```text
NorthboundDemo!2026
```

## Lantern & Leaf Society

This broad social reading group demonstrates public team comparisons, an inherited current-month announcement, an archived historical month, and an enrolled-but-unassigned reader.

| Username | Name | Group role |
| --- | --- | --- |
| `maren.holt` | Maren Holt | Group Owner |
| `theo.bennett` | Theo Bennett | Moderator |
| `priya.shah` | Priya Shah | Moderator |
| `caleb.ross` | Caleb Ross | Member |
| `lena.ortiz` | Lena Ortiz | Member |
| `nora.kim` | Nora Kim | Member |
| `elliot.price` | Elliot Price | Member |
| `jasmine.cole` | Jasmine Cole | Member |
| `wesley.grant` | Wesley Grant | Member |
| `fiona.brooks` | Fiona Brooks | Member, currently unassigned |
| `jonah.vale` | Jonah Vale | Cross-group Member |

## Midnight Quill Guild

This atmospheric genre group demonstrates staff-only team statistics, a custom current-month announcement, a finalized historical month, and different cross-group roles.

| Username | Name | Group role |
| --- | --- | --- |
| `celeste.rowan` | Celeste Rowan | Group Owner |
| `jonah.vale` | Jonah Vale | Moderator |
| `amara.quinn` | Amara Quinn | Moderator |
| `miles.arden` | Miles Arden | Member |
| `ivy.mercer` | Ivy Mercer | Member |
| `dante.frost` | Dante Frost | Member |
| `soraya.bell` | Soraya Bell | Member |
| `lucas.wren` | Lucas Wren | Member |
| `opal.rivera` | Opal Rivera | Member |
| `henry.sloane` | Henry Sloane | Member, currently unassigned |
| `nora.kim` | Nora Kim | Cross-group Member |

The dataset includes two challenge months per group, two teams and two themes per month, 20 fictional catalog books, 34 submissions, approved and pending theme claims, unequal team scores, historical team changes, review queues, and safe audit activity. It uses no external API calls, downloaded covers, or filesystem paths outside normal Northbound data storage.

A normal SQLite Stored Backup will include this database content. Platform Owner state is deliberately not managed by the seeder, but a full Stored Backup still contains the installation database and therefore includes whatever Platform Owner state exists when the backup is created.
