from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class BotmSchemaMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0044_games_schema_foundation")]
    migrate_to = [("core", "0045_botm_schema_foundation")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        self.record_ids = self._create_existing_data(old_apps)

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _create_existing_data(self, apps):
        ReadingGroup = apps.get_model("core", "ReadingGroup")
        ChallengeMonth = apps.get_model("core", "ChallengeMonth")
        Game = apps.get_model("core", "Game")

        group = ReadingGroup.objects.create(name="Pre-BOTM Group", slug="pre-botm-group")
        month = ChallengeMonth.objects.create(
            group=group,
            name="Pre-BOTM Challenge",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            games_enabled=True,
        )
        game = Game.objects.create(
            month=month,
            name="Preserved Game",
            starts_at="2026-09-01T12:00:00Z",
            ends_at="2026-09-02T12:00:00Z",
            advertised_bonus_pages=20,
        )
        return {"month": month.pk, "game": game.pk}

    def test_0045_preserves_existing_data_and_creates_no_botm_books(self):
        ChallengeMonth = self.apps.get_model("core", "ChallengeMonth")
        Game = self.apps.get_model("core", "Game")
        BotmBook = self.apps.get_model("core", "BotmBook")

        month = ChallengeMonth.objects.get(pk=self.record_ids["month"])
        self.assertTrue(month.games_enabled)
        self.assertFalse(month.botm_enabled)
        self.assertEqual(month.botm_completion_bonus_pages, 0)
        self.assertEqual(Game.objects.get(pk=self.record_ids["game"]).name, "Preserved Game")
        self.assertFalse(BotmBook.objects.exists())
