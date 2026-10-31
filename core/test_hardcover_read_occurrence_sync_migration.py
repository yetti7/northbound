from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class HardcoverReadOccurrenceMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0057_hardcover_completed_book_sync")]
    migrate_to = [("core", "0058_hardcover_read_occurrence_sync")]

    def test_existing_hc4_rows_gain_empty_occurrence_mapping_fields(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model("auth", "User")
        Outbox = old_apps.get_model("core", "HardcoverSyncOutbox")
        Provenance = old_apps.get_model("core", "HardcoverSyncProvenance")
        user = User.objects.create(username="hc5-migration-reader")
        outbox = Outbox.objects.create(
            user_id=user.pk, event_key="book_submission:51:completed_book:v1",
            source_type="BookSubmission", source_id="51", action="completed_book",
            provider_book_id="123", provider_user_book_id="900", result_detail="created",
        )
        Provenance.objects.create(
            outbox_id=outbox.pk, user_id=user.pk, attempt_number=1,
            source_type="BookSubmission", source_id="51", action="completed_book",
            outcome="succeeded", provider_book_id="123", provider_identifier="900",
            result_detail="created",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        migrated_outbox = apps.get_model("core", "HardcoverSyncOutbox").objects.get(pk=outbox.pk)
        migrated_attempt = apps.get_model("core", "HardcoverSyncProvenance").objects.get(outbox_id=outbox.pk)
        self.assertEqual(migrated_outbox.provider_read_id, "")
        self.assertEqual(migrated_outbox.library_result_detail, "")
        self.assertEqual(migrated_outbox.occurrence_result_detail, "")
        self.assertEqual(migrated_attempt.provider_read_id, "")
        self.assertEqual(migrated_attempt.library_result_detail, "")
        self.assertEqual(migrated_attempt.occurrence_result_detail, "")
