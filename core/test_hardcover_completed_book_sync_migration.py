from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class HardcoverCompletedBookSyncMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0056_hardcover_sync_foundation")]
    migrate_to = [("core", "0057_hardcover_completed_book_sync")]

    def test_existing_outbox_and_provenance_gain_empty_safe_result_fields(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model("auth", "User")
        Outbox = old_apps.get_model("core", "HardcoverSyncOutbox")
        Provenance = old_apps.get_model("core", "HardcoverSyncProvenance")
        user = User.objects.create(username="migration-reader")
        outbox = Outbox.objects.create(
            user_id=user.pk, event_key="book_submission:9:completed_book:v1",
            source_type="BookSubmission", source_id="9", action="completed_book",
            effective_date=date(2026, 9, 1),
        )
        Provenance.objects.create(
            outbox_id=outbox.pk, user_id=user.pk, attempt_number=1,
            source_type="BookSubmission", source_id="9", action="completed_book",
            effective_date=date(2026, 9, 1), outcome="skipped",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        migrated_outbox = apps.get_model("core", "HardcoverSyncOutbox").objects.get(pk=outbox.pk)
        migrated_attempt = apps.get_model("core", "HardcoverSyncProvenance").objects.get(outbox_id=outbox.pk)
        self.assertEqual(migrated_outbox.provider_book_id, "")
        self.assertEqual(migrated_outbox.provider_user_book_id, "")
        self.assertEqual(migrated_outbox.result_detail, "")
        self.assertEqual(migrated_attempt.provider_book_id, "")
        self.assertEqual(migrated_attempt.result_detail, "")
