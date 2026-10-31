from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class HardcoverSyncFoundationMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0055_reader_hardcover_oauth_credentials")]
    migrate_to = [("core", "0056_hardcover_sync_foundation")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model("auth", "User")
        self.user_id = User.objects.create(username="existing-hc3-reader").pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_0056_preserves_existing_reader_with_no_consent_or_queued_work(self):
        User = self.apps.get_model("auth", "User")
        Preference = self.apps.get_model("core", "ReaderHardcoverSyncPreference")
        Outbox = self.apps.get_model("core", "HardcoverSyncOutbox")
        Provenance = self.apps.get_model("core", "HardcoverSyncProvenance")

        self.assertTrue(User.objects.filter(pk=self.user_id).exists())
        self.assertFalse(Preference.objects.exists())
        self.assertFalse(Outbox.objects.exists())
        self.assertFalse(Provenance.objects.exists())
