from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .models import (
    BookSubmission,
    ChallengeMonth,
    Membership,
    ModifierProvenance,
    MonthEnrollment,
    MonthTheme,
    ProgressCheckpoint,
    ProgressCheckpointResult,
    ReadingGroup,
    Team,
    TeamAssignment,
    ThemeClaim,
)
from .scoring import refresh_submission_score, theme_approval_snapshot


class SubmissionScoringServiceTests(TestCase):
    def setUp(self):
        self.reviewer = get_user_model().objects.create_user("scoring-reviewer")
        reader = get_user_model().objects.create_user("scoring-reader")
        self.group = ReadingGroup.objects.create(name="Scoring Group", slug="scoring-group")
        self.participant = Membership.objects.create(
            group=self.group,
            user=reader,
            role=Membership.Role.MEMBER,
            display_name="Scoring Reader",
        )
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Scoring Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
        )
        self.theme_one = MonthTheme.objects.create(bonus_rule="legacy_fixed",
            month=self.month,
            name="First Theme",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            bonus_pages=25,
        )
        self.theme_two = MonthTheme.objects.create(bonus_rule="legacy_fixed",
            month=self.month,
            name="Second Theme",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            bonus_pages=50,
        )

    def submission(self, title="Scored Book", *, status=BookSubmission.Status.APPROVED, removed=False):
        return BookSubmission.objects.create(
            month=self.month,
            participant=self.participant,
            title=title,
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 8, 15),
            submitted_pages=200,
            approved_pages=200,
            status=status,
            is_removed=removed,
        )

    def claim(self, submission, theme, amount, *, status=ThemeClaim.Status.APPROVED):
        return ThemeClaim.objects.create(
            submission=submission,
            theme=theme,
            status=status,
            approved_bonus_pages=amount,
            reviewed_by=self.reviewer,
            reviewed_at=timezone.now(),
        )

    def test_theme_and_legacy_sources_sum_and_repeated_refresh_is_idempotent(self):
        submission = self.submission()
        first_claim = self.claim(submission, self.theme_one, 25)
        second_claim = self.claim(submission, self.theme_two, 50)
        migrated = ModifierProvenance.objects.create(
            month=self.month,
            participant=self.participant,
            submission=submission,
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
            source_reference=f"theme_claim:{first_claim.pk}",
            source_label="First Theme",
            source_context="Migrated approved Theme claim",
            amount=25,
            effective_date=submission.completed_on,
            is_system_generated=True,
        )
        ModifierProvenance.objects.create(
            month=self.month,
            participant=self.participant,
            submission=submission,
            source_type=ModifierProvenance.SourceType.LEGACY_MODIFIER,
            source_reference=f"legacy_submission:{submission.pk}",
            source_label="Legacy Modifier",
            amount=10,
            effective_date=submission.completed_on,
            is_system_generated=True,
        )

        self.assertEqual(refresh_submission_score(submission), 285)
        submission.refresh_from_db()
        self.assertEqual((submission.approved_pages, submission.bonus_pages, submission.final_scored_pages), (200, 85, 285))
        self.assertEqual(
            ModifierProvenance.objects.filter(
                submission=submission,
                source_type=ModifierProvenance.SourceType.THEME_BONUS,
            ).count(),
            2,
        )
        self.assertEqual(ModifierProvenance.objects.get(pk=migrated.pk).amount, 25)

        refresh_submission_score(submission)
        self.assertEqual(ModifierProvenance.objects.filter(submission=submission).count(), 3)
        second_source = ModifierProvenance.objects.get(source_reference=f"theme_claim:{second_claim.pk}")
        self.assertEqual(second_source.amount, 50)

    def test_pending_and_rejected_claims_deactivate_then_reuse_the_same_source(self):
        submission = self.submission()
        claim = self.claim(submission, self.theme_one, 25)
        refresh_submission_score(submission)
        source = ModifierProvenance.objects.get(source_reference=f"theme_claim:{claim.pk}")

        claim.status = ThemeClaim.Status.REJECTED
        claim.approved_bonus_pages = 0
        claim.save(update_fields=["status", "approved_bonus_pages"])
        refresh_submission_score(submission)
        submission.refresh_from_db()
        source.refresh_from_db()
        self.assertFalse(source.is_active)
        self.assertEqual((submission.bonus_pages, submission.final_scored_pages), (0, 200))

        claim.status = ThemeClaim.Status.PENDING
        claim.save(update_fields=["status"])
        refresh_submission_score(submission)
        self.assertFalse(ModifierProvenance.objects.get(pk=source.pk).is_active)

        claim.status = ThemeClaim.Status.APPROVED
        claim.approved_bonus_pages = theme_approval_snapshot(claim)
        claim.save(update_fields=["status", "approved_bonus_pages"])
        refresh_submission_score(submission)
        submission.refresh_from_db()
        source.refresh_from_db()
        self.assertTrue(source.is_active)
        self.assertEqual(source.amount, 25)
        self.assertEqual((submission.bonus_pages, submission.final_scored_pages), (25, 225))
        self.assertEqual(ModifierProvenance.objects.filter(source_reference=f"theme_claim:{claim.pk}").count(), 1)

    def test_historical_snapshot_survives_rejection_and_later_theme_edit(self):
        submission = self.submission()
        claim = self.claim(submission, self.theme_one, 25)
        refresh_submission_score(submission)

        claim.status = ThemeClaim.Status.REJECTED
        claim.approved_bonus_pages = 0
        claim.save(update_fields=["status", "approved_bonus_pages"])
        refresh_submission_score(submission)
        self.theme_one.bonus_pages = 50
        self.theme_one.save(update_fields=["bonus_pages"])

        self.assertEqual(theme_approval_snapshot(claim), 25)
        claim.status = ThemeClaim.Status.APPROVED
        claim.approved_bonus_pages = theme_approval_snapshot(claim)
        claim.save(update_fields=["status", "approved_bonus_pages"])
        refresh_submission_score(submission)
        submission.refresh_from_db()
        self.assertEqual((claim.approved_bonus_pages, submission.bonus_pages, submission.final_scored_pages), (25, 25, 225))

    def test_removed_submission_preserves_but_inactivates_provenance_and_remains_out_of_team_total(self):
        MonthEnrollment.objects.create(month=self.month, participant=self.participant)
        team = Team.objects.create(month=self.month, name="Scoring Team")
        TeamAssignment.objects.create(month=self.month, team=team, participant=self.participant)
        submission = self.submission()
        claim = self.claim(submission, self.theme_one, 25)

        refresh_submission_score(submission)
        submission.is_removed = True
        submission.save(update_fields=["is_removed"])
        refresh_submission_score(submission)
        source = ModifierProvenance.objects.get(source_reference=f"theme_claim:{claim.pk}")
        self.assertFalse(source.is_active)
        self.assertTrue(BookSubmission.objects.get(pk=submission.pk).is_removed)
        self.assertEqual(team.approved_pages, 0)

    def test_nonapproved_cache_behavior_and_checkpoint_snapshot_are_unchanged(self):
        submission = self.submission()
        claim = self.claim(submission, self.theme_one, 25)
        refresh_submission_score(submission)
        checkpoint = ProgressCheckpoint.objects.create(
            month=self.month,
            scheduled_at=timezone.now(),
            progress_basis=ProgressCheckpoint.ProgressBasis.TOTAL,
            target_basis=ProgressCheckpoint.TargetBasis.FIXED,
            fixed_target_pages=1000,
            evaluation_state=ProgressCheckpoint.EvaluationState.EVALUATED,
            evaluated_at=timezone.now(),
        )
        result = ProgressCheckpointResult.objects.create(
            checkpoint=checkpoint,
            participant=self.participant,
            evaluated_at=timezone.now(),
            threshold_percentage=25,
            progress_basis=ProgressCheckpoint.ProgressBasis.TOTAL,
            target_basis=ProgressCheckpoint.TargetBasis.FIXED,
            target_pages=Decimal("1000"),
            required_pages=Decimal("250"),
            progress_pages=225,
            outcome=ProgressCheckpointResult.Outcome.BELOW,
        )

        submission.status = BookSubmission.Status.REJECTED
        submission.save(update_fields=["status", "bonus_pages", "final_scored_pages"])
        refresh_submission_score(submission)
        submission.refresh_from_db()
        result.refresh_from_db()
        self.assertEqual(submission.approved_pages, 200)
        self.assertEqual(submission.bonus_pages, 0)
        self.assertIsNone(submission.final_scored_pages)
        self.assertFalse(ModifierProvenance.objects.get(source_reference=f"theme_claim:{claim.pk}").is_active)
        self.assertEqual((result.progress_pages, result.outcome), (225, ProgressCheckpointResult.Outcome.BELOW))

    def test_recalculate_score_compatibility_wrapper_uses_the_service(self):
        submission = self.submission()
        self.claim(submission, self.theme_one, 25)
        self.assertEqual(submission.recalculate_score(save=False), 225)
        self.assertFalse(ModifierProvenance.objects.filter(submission=submission).exists())
        self.assertEqual(submission.recalculate_score(), 225)
        submission.refresh_from_db()
        self.assertEqual((submission.bonus_pages, submission.final_scored_pages), (25, 225))

    def test_malformed_existing_theme_source_is_reported_not_silently_reassigned(self):
        submission = self.submission()
        claim = self.claim(submission, self.theme_one, 25)
        other_user = get_user_model().objects.create_user("other-scoring-reader")
        other_participant = Membership.objects.create(
            group=self.group,
            user=other_user,
            role=Membership.Role.MEMBER,
            display_name="Other Reader",
        )
        ModifierProvenance.objects.create(
            month=self.month,
            participant=other_participant,
            submission=None,
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
            source_reference=f"theme_claim:{claim.pk}",
            source_label="Malformed Theme Source",
            amount=25,
            effective_date=submission.completed_on,
        )

        with self.assertRaisesMessage(ValidationError, "does not belong to its claim"):
            refresh_submission_score(submission)
        submission.refresh_from_db()
        self.assertEqual((submission.bonus_pages, submission.final_scored_pages), (0, 200))

    def test_rejected_claim_still_reports_malformed_existing_theme_source(self):
        submission = self.submission()
        claim = self.claim(submission, self.theme_one, 0, status=ThemeClaim.Status.REJECTED)
        other_submission = self.submission("Wrongly Linked Book")
        malformed = ModifierProvenance.objects.create(
            month=self.month,
            participant=self.participant,
            submission=other_submission,
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
            source_reference=f"theme_claim:{claim.pk}",
            source_label="Malformed Theme Source",
            amount=25,
            effective_date=other_submission.completed_on,
        )

        with self.assertRaisesMessage(ValidationError, "does not belong to its claim"):
            refresh_submission_score(submission)
        malformed.refresh_from_db()
        self.assertTrue(malformed.is_active)
