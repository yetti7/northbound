from datetime import date, datetime, timezone
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class GamesSchemaMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0043_modifier_provenance_foundation")]
    migrate_to = [("core", "0044_games_schema_foundation")]

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
        User = apps.get_model("auth", "User")
        ReadingGroup = apps.get_model("core", "ReadingGroup")
        Membership = apps.get_model("core", "Membership")
        ChallengeMonth = apps.get_model("core", "ChallengeMonth")
        BookSubmission = apps.get_model("core", "BookSubmission")
        ModifierProvenance = apps.get_model("core", "ModifierProvenance")
        ProgressCheckpoint = apps.get_model("core", "ProgressCheckpoint")
        ProgressCheckpointResult = apps.get_model("core", "ProgressCheckpointResult")

        user = User.objects.create(username="pre-games-reader")
        group = ReadingGroup.objects.create(name="Pre-Games Group", slug="pre-games-group")
        participant = Membership.objects.create(group=group, user=user, role="member", display_name="Pre-Games Reader")
        month = ChallengeMonth.objects.create(
            group=group,
            name="Pre-Games Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status="active",
        )
        submission = BookSubmission.objects.create(
            month=month,
            participant=participant,
            title="Preserved Book",
            author="Author",
            book_format="ebook",
            completed_on=date(2026, 8, 10),
            submitted_pages=200,
            approved_pages=200,
            bonus_pages=25,
            final_scored_pages=225,
            status="approved",
        )
        provenance = ModifierProvenance.objects.create(
            month=month,
            participant=participant,
            submission=submission,
            source_type="legacy_modifier",
            source_reference=f"legacy_submission:{submission.pk}",
            source_label="Legacy Modifier",
            source_context="Preserved source",
            amount=25,
            effective_date=date(2026, 8, 10),
            is_system_generated=True,
        )
        evaluated_at = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
        checkpoint = ProgressCheckpoint.objects.create(
            month=month,
            scheduled_at=evaluated_at,
            threshold_percentage=25,
            progress_basis="total",
            target_basis="fixed",
            fixed_target_pages=1000,
            evaluation_state="evaluated",
            evaluated_at=evaluated_at,
        )
        result = ProgressCheckpointResult.objects.create(
            checkpoint=checkpoint,
            participant=participant,
            evaluated_at=evaluated_at,
            threshold_percentage=25,
            progress_basis="total",
            target_basis="fixed",
            target_pages=Decimal("1000"),
            required_pages=Decimal("250"),
            progress_pages=225,
            outcome="below",
        )
        return {"month": month.pk, "submission": submission.pk, "provenance": provenance.pk, "result": result.pk}

    def test_0044_preserves_existing_data_and_creates_no_games_history(self):
        ChallengeMonth = self.apps.get_model("core", "ChallengeMonth")
        BookSubmission = self.apps.get_model("core", "BookSubmission")
        ModifierProvenance = self.apps.get_model("core", "ModifierProvenance")
        ProgressCheckpointResult = self.apps.get_model("core", "ProgressCheckpointResult")
        Game = self.apps.get_model("core", "Game")
        GameRewardApplication = self.apps.get_model("core", "GameRewardApplication")
        GameRewardRecipient = self.apps.get_model("core", "GameRewardRecipient")

        month = ChallengeMonth.objects.get(pk=self.record_ids["month"])
        self.assertFalse(month.games_enabled)
        submission = BookSubmission.objects.get(pk=self.record_ids["submission"])
        self.assertEqual((submission.approved_pages, submission.bonus_pages, submission.final_scored_pages), (200, 25, 225))
        provenance = ModifierProvenance.objects.get(pk=self.record_ids["provenance"])
        self.assertEqual((provenance.source_type, provenance.amount, provenance.is_active), ("legacy_modifier", 25, True))
        result = ProgressCheckpointResult.objects.get(pk=self.record_ids["result"])
        self.assertEqual((result.progress_pages, result.outcome), (225, "below"))
        self.assertFalse(Game.objects.exists())
        self.assertFalse(GameRewardApplication.objects.exists())
        self.assertFalse(GameRewardRecipient.objects.exists())
