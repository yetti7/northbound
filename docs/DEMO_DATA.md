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
| `theo.bennett` | Theo Bennett | Administrator |
| `priya.shah` | Priya Shah | Moderator |
| `caleb.ross` | Caleb Ross | Game Manager |
| `lena.ortiz` | Lena Ortiz | Reader |
| `nora.kim` | Nora Kim | Reader |
| `elliot.price` | Elliot Price | Reader |
| `jasmine.cole` | Jasmine Cole | Reader |
| `wesley.grant` | Wesley Grant | Reader |
| `fiona.brooks` | Fiona Brooks | Reader, currently unassigned |
| `jonah.vale` | Jonah Vale | Cross-group Reader |

## Midnight Quill Guild

This atmospheric genre group demonstrates staff-only team statistics, a custom current-month announcement, a finalized historical month, and different cross-group roles.

| Username | Name | Group role |
| --- | --- | --- |
| `celeste.rowan` | Celeste Rowan | Group Owner |
| `jonah.vale` | Jonah Vale | Administrator |
| `amara.quinn` | Amara Quinn | Moderator |
| `miles.arden` | Miles Arden | Game Manager |
| `ivy.mercer` | Ivy Mercer | Reader |
| `dante.frost` | Dante Frost | Reader |
| `soraya.bell` | Soraya Bell | Reader |
| `lucas.wren` | Lucas Wren | Reader |
| `opal.rivera` | Opal Rivera | Reader |
| `henry.sloane` | Henry Sloane | Reader, currently unassigned |
| `nora.kim` | Nora Kim | Cross-group Game Manager |

The dataset includes two challenge months per group, two teams and two themes per month, 20 fictional catalog books, 34 submissions, approved and pending theme claims, unequal team scores, historical team changes, review queues, and safe audit activity. It uses no external API calls, downloaded covers, or filesystem paths outside normal Northbound data storage.

A normal SQLite Stored Backup will include this database content. Platform Owner state is deliberately not managed by the seeder, but a full Stored Backup still contains the installation database and therefore includes whatever Platform Owner state exists when the backup is created.
