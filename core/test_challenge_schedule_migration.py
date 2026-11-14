from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class ChallengeScheduleMigrationTests(TransactionTestCase):
    def test_existing_schedule_is_preserved_and_only_due_registration_events_are_consumed(self):
        before = [("core", "0066_monthenrollment_host_scope_restored")]
        after = [("core", "0067_challenge_schedule_state")]
        executor = MigrationExecutor(connection)
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(executor.loader.graph.leaf_nodes()))
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        group = old.get_model("core", "ReadingGroup").objects.create(name="Schedule", slug="schedule")
        challenge_model = old.get_model("core", "ChallengeMonth")
        past = timezone.now() - timedelta(days=1)
        future = past + timedelta(days=2)
        challenge = challenge_model.objects.create(
            group=group, name="Existing schedule", status="active", registration_is_open=True,
            registration_opens_at=past, registration_closes_at=past,
            starts_at=past, ends_at=future, auto_complete_challenge=True,
            final_announcement_at=future,
        )
        other = challenge_model.objects.create(
            group=group, name="Mixed schedule", status="upcoming",
            registration_opens_at=past, registration_closes_at=future, auto_open_registration=False,
        )
        original = challenge_model.objects.filter(pk=challenge.pk).values().get()
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        new = executor.loader.project_state(after).apps.get_model("core", "ChallengeMonth")
        migrated = new.objects.filter(pk=challenge.pk).values().get()
        self.assertTrue(migrated.pop("registration_open_handled"))
        self.assertTrue(migrated.pop("registration_close_handled"))
        self.assertEqual(migrated.pop("held_schedule_milestones"), [])
        self.assertEqual(migrated, original)
        mixed = new.objects.get(pk=other.pk)
        self.assertFalse(mixed.registration_open_handled)
        self.assertFalse(mixed.registration_close_handled)
