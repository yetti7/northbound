from datetime import date, datetime, timezone
from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase


class ModifierProvenanceMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0042_challenge_role_visibility_audiences")]
    migrate_to = [("core", "0043_modifier_provenance_foundation")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        self.record_ids = self._create_historical_records(old_apps)

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _create_historical_records(self, apps):
        User = apps.get_model("auth", "User")
        ReadingGroup = apps.get_model("core", "ReadingGroup")
        Membership = apps.get_model("core", "Membership")
        ChallengeMonth = apps.get_model("core", "ChallengeMonth")
        BookSubmission = apps.get_model("core", "BookSubmission")
        MonthTheme = apps.get_model("core", "MonthTheme")
        ThemeClaim = apps.get_model("core", "ThemeClaim")
        ProgressCheckpoint = apps.get_model("core", "ProgressCheckpoint")
        ProgressCheckpointResult = apps.get_model("core", "ProgressCheckpointResult")

        reviewer = User.objects.create(username="provenance-reviewer")
        reader = User.objects.create(username="provenance-reader")
        group = ReadingGroup.objects.create(name="Provenance Group", slug="provenance-group")
        participant = Membership.objects.create(
            group=group,
            user=reader,
            role="member",
            display_name="Provenance Reader",
        )
        month = ChallengeMonth.objects.create(
            group=group,
            name="Provenance Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status="active",
        )
        reviewed_at = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

        def submission(title, bonus, *, removed=False):
            return BookSubmission.objects.create(
                month=month,
                participant=participant,
                title=title,
                author="Author",
                book_format="ebook",
                completed_on=date(2026, 8, 15),
                submitted_pages=200,
                approved_pages=200,
                bonus_pages=bonus,
                final_scored_pages=200 + bonus,
                status="approved",
                reviewed_by=reviewer,
                reviewed_at=reviewed_at,
                is_removed=removed,
            )

        theme_one = MonthTheme.objects.create(
            month=month,
            name="Theme One",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            bonus_pages=999,
        )
        theme_two = MonthTheme.objects.create(
            month=month,
            name="Theme Two",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            bonus_pages=999,
        )

        single = submission("Single Theme", 30)
        single_claim = ThemeClaim.objects.create(
            submission=single,
            theme=theme_one,
            status="approved",
            approved_bonus_pages=30,
            reviewed_by=reviewer,
            reviewed_at=reviewed_at,
        )

        stacked = submission("Stacked Themes", 30)
        stacked_claims = [
            ThemeClaim.objects.create(
                submission=stacked,
                theme=theme,
                status="approved",
                approved_bonus_pages=amount,
                reviewed_by=reviewer,
                reviewed_at=reviewed_at,
            )
            for theme, amount in ((theme_one, 10), (theme_two, 20))
        ]

        inactive_claims = submission("Inactive Claims", 0)
        ThemeClaim.objects.create(
            submission=inactive_claims,
            theme=theme_one,
            status="pending",
            approved_bonus_pages=0,
        )
        ThemeClaim.objects.create(
            submission=inactive_claims,
            theme=theme_two,
            status="rejected",
            approved_bonus_pages=50,
        )

        zero_bonus = submission("Zero Approved Bonus", 0)
        zero_claim = ThemeClaim.objects.create(
            submission=zero_bonus,
            theme=theme_one,
            status="approved",
            approved_bonus_pages=0,
        )

        removed = submission("Removed Theme", 40, removed=True)
        removed_claim = ThemeClaim.objects.create(
            submission=removed,
            theme=theme_one,
            status="approved",
            approved_bonus_pages=40,
            reviewed_by=reviewer,
            reviewed_at=reviewed_at,
        )
        legacy = submission("Legacy Difference", 25)

        checkpoint = ProgressCheckpoint.objects.create(
            month=month,
            scheduled_at=reviewed_at,
            threshold_percentage=25,
            progress_basis="total",
            target_basis="fixed",
            fixed_target_pages=1000,
            evaluation_state="evaluated",
            evaluated_at=reviewed_at,
        )
        checkpoint_result = ProgressCheckpointResult.objects.create(
            checkpoint=checkpoint,
            participant=participant,
            evaluated_at=reviewed_at,
            threshold_percentage=25,
            progress_basis="total",
            target_basis="fixed",
            target_pages=Decimal("1000"),
            required_pages=Decimal("250"),
            progress_pages=260,
            outcome="met",
        )
        return {
            "single": single.pk,
            "single_claim": single_claim.pk,
            "stacked": stacked.pk,
            "stacked_claims": [claim.pk for claim in stacked_claims],
            "inactive_claims": inactive_claims.pk,
            "zero_claim": zero_claim.pk,
            "removed": removed.pk,
            "removed_claim": removed_claim.pk,
            "legacy": legacy.pk,
            "checkpoint_result": checkpoint_result.pk,
        }

    def test_backfill_preserves_scores_and_creates_idempotent_theme_and_legacy_sources(self):
        BookSubmission = self.apps.get_model("core", "BookSubmission")
        ThemeClaim = self.apps.get_model("core", "ThemeClaim")
        ModifierProvenance = self.apps.get_model("core", "ModifierProvenance")
        ProgressCheckpointResult = self.apps.get_model("core", "ProgressCheckpointResult")

        single_sources = ModifierProvenance.objects.filter(submission_id=self.record_ids["single"])
        self.assertEqual(single_sources.count(), 1)
        single_source = single_sources.get()
        self.assertEqual(single_source.source_type, "theme_bonus")
        self.assertEqual(single_source.source_reference, f"theme_claim:{self.record_ids['single_claim']}")
        self.assertEqual(single_source.amount, 30)
        self.assertEqual(single_source.effective_date, date(2026, 8, 15))
        self.assertTrue(single_source.is_active)
        self.assertTrue(single_source.is_system_generated)

        stacked_sources = ModifierProvenance.objects.filter(submission_id=self.record_ids["stacked"])
        self.assertEqual(stacked_sources.count(), 2)
        self.assertEqual(set(stacked_sources.values_list("amount", flat=True)), {10, 20})
        self.assertEqual(
            set(stacked_sources.values_list("source_reference", flat=True)),
            {f"theme_claim:{claim_id}" for claim_id in self.record_ids["stacked_claims"]},
        )

        self.assertFalse(ModifierProvenance.objects.filter(submission_id=self.record_ids["inactive_claims"]).exists())
        self.assertFalse(ModifierProvenance.objects.filter(source_reference=f"theme_claim:{self.record_ids['zero_claim']}").exists())

        removed_source = ModifierProvenance.objects.get(submission_id=self.record_ids["removed"])
        self.assertEqual(removed_source.source_reference, f"theme_claim:{self.record_ids['removed_claim']}")
        self.assertTrue(BookSubmission.objects.get(pk=self.record_ids["removed"]).is_removed)

        legacy_source = ModifierProvenance.objects.get(submission_id=self.record_ids["legacy"])
        self.assertEqual(legacy_source.source_type, "legacy_modifier")
        self.assertEqual(legacy_source.source_label, "Legacy Modifier")
        self.assertEqual(legacy_source.amount, 25)
        self.assertIsNone(legacy_source.applied_by_id)
        self.assertTrue(legacy_source.is_system_generated)

        expected_scores = {
            self.record_ids["single"]: (200, 30, 230),
            self.record_ids["stacked"]: (200, 30, 230),
            self.record_ids["removed"]: (200, 40, 240),
            self.record_ids["legacy"]: (200, 25, 225),
        }
        for submission_id, expected in expected_scores.items():
            submission = BookSubmission.objects.get(pk=submission_id)
            self.assertEqual(
                (submission.approved_pages, submission.bonus_pages, submission.final_scored_pages),
                expected,
            )
        self.assertEqual(
            ThemeClaim.objects.get(pk=self.record_ids["single_claim"]).approved_bonus_pages,
            30,
        )
        checkpoint_result = ProgressCheckpointResult.objects.get(pk=self.record_ids["checkpoint_result"])
        self.assertEqual((checkpoint_result.progress_pages, checkpoint_result.outcome), (260, "met"))


class ModifierProvenanceConstraintTests(TestCase):
    def test_duplicate_source_reference_is_rejected_and_submission_is_optional(self):
        from django.contrib.auth import get_user_model

        from core.models import ChallengeMonth, Membership, ModifierProvenance, ReadingGroup

        user = get_user_model().objects.create_user("modifier-constraint")
        group = ReadingGroup.objects.create(name="Constraint Group", slug="constraint-group")
        participant = Membership.objects.create(
            group=group,
            user=user,
            role=Membership.Role.MEMBER,
            display_name="Constraint Reader",
        )
        month = ChallengeMonth.objects.create(
            group=group,
            name="Constraint Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
        )
        standalone_values = {
            "month": month,
            "participant": participant,
            "submission": None,
            "source_type": ModifierProvenance.SourceType.GAME_REWARD,
            "source_reference": "future-game-reward:1",
            "source_label": "Standalone reward",
            "amount": 10,
            "effective_date": date(2026, 8, 15),
        }
        ModifierProvenance.objects.create(**standalone_values)
        theme_values = {
            **standalone_values,
            "source_type": ModifierProvenance.SourceType.THEME_BONUS,
            "source_reference": "theme_claim:999",
            "source_label": "Theme snapshot",
        }
        ModifierProvenance.objects.create(**theme_values)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ModifierProvenance.objects.create(**theme_values)


class ModifierProvenanceIntegrityMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0042_challenge_role_visibility_audiences")]
    migrate_to = [("core", "0043_modifier_provenance_foundation")]

    def test_theme_snapshots_above_stored_bonus_stop_without_changing_scores(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        apps = executor.loader.project_state(self.migrate_from).apps
        User = apps.get_model("auth", "User")
        ReadingGroup = apps.get_model("core", "ReadingGroup")
        Membership = apps.get_model("core", "Membership")
        ChallengeMonth = apps.get_model("core", "ChallengeMonth")
        BookSubmission = apps.get_model("core", "BookSubmission")
        MonthTheme = apps.get_model("core", "MonthTheme")
        ThemeClaim = apps.get_model("core", "ThemeClaim")

        user = User.objects.create(username="inconsistent-provenance")
        group = ReadingGroup.objects.create(name="Integrity Group", slug="integrity-group")
        participant = Membership.objects.create(
            group=group,
            user=user,
            role="member",
            display_name="Integrity Reader",
        )
        month = ChallengeMonth.objects.create(
            group=group,
            name="Integrity Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status="active",
        )
        submission = BookSubmission.objects.create(
            month=month,
            participant=participant,
            title="Inconsistent Book",
            author="Author",
            book_format="ebook",
            completed_on=date(2026, 8, 15),
            submitted_pages=200,
            approved_pages=200,
            bonus_pages=20,
            final_scored_pages=220,
            status="approved",
        )
        theme = MonthTheme.objects.create(
            month=month,
            name="Integrity Theme",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
        )
        ThemeClaim.objects.create(
            submission=submission,
            theme=theme,
            status="approved",
            approved_bonus_pages=50,
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(RuntimeError, "approved Theme claim snapshots exceed"):
                executor.migrate(self.migrate_to)
            submission.refresh_from_db()
            self.assertEqual((submission.bonus_pages, submission.final_scored_pages), (20, 220))
        finally:
            # Restore the migration graph for the remaining test run without
            # hiding the condition that caused the expected migration stop.
            BookSubmission.objects.filter(pk=submission.pk).update(
                bonus_pages=50,
                final_scored_pages=250,
            )
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
