from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    BookSubmission,
    ChallengeMonth,
    ChallengeStaffAssignment,
    Membership,
    ModifierProvenance,
    MonthEnrollment,
    MonthTheme,
    ProgressCheckpoint,
    ProgressCheckpointResult,
    ReadingGroup,
    ThemeClaim,
)


class ThemeReviewIntegrationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.host_user = User.objects.create_user("theme-review-host", password="test-password")
        reader_user = User.objects.create_user("theme-review-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Theme Review Group", slug="theme-review-group")
        host = Membership.objects.create(group=self.group, user=self.host_user, display_name="Host")
        self.reader = Membership.objects.create(group=self.group, user=reader_user, display_name="Reader")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Theme Review Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=host,
            role=ChallengeStaffAssignment.Role.HOST,
        )
        MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        self.theme = self.make_theme("First Theme", 25)
        self.submission = BookSubmission.objects.create(
            month=self.month,
            participant=self.reader,
            title="Theme Review Book",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 8, 15),
            submitted_pages=200,
        )
        self.claim = ThemeClaim.objects.create(submission=self.submission, theme=self.theme)
        self.client.force_login(self.host_user)

    def make_theme(self, name, amount):
        return MonthTheme.objects.create(bonus_rule="legacy_fixed",
            month=self.month,
            name=name,
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            bonus_pages=amount,
        )

    def review(self, submission_status, claim_statuses, *, approved_pages=200):
        claims = list(self.submission.theme_claims.order_by("pk"))
        data = {
            "approved_pages": approved_pages,
            "status": submission_status,
            "verification_url": "",
            "review_notes": "Theme integration review",
            "claims-TOTAL_FORMS": str(len(claims)),
            "claims-INITIAL_FORMS": str(len(claims)),
            "claims-MIN_NUM_FORMS": "0",
            "claims-MAX_NUM_FORMS": "1000",
        }
        for index, claim in enumerate(claims):
            data.update({
                f"claims-{index}-id": str(claim.pk),
                f"claims-{index}-submission": str(self.submission.pk),
                f"claims-{index}-status": claim_statuses[claim.pk],
            })
        response = self.client.post(
            reverse("submission-review", kwargs={
                "group_slug": self.group.slug,
                "month_pk": self.month.pk,
                "pk": self.submission.pk,
            }),
            data,
        )
        self.assertRedirects(
            response,
            reverse("review-queue", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}),
        )
        self.submission.refresh_from_db()
        for claim in claims:
            claim.refresh_from_db()

    def source(self, claim=None):
        claim = claim or self.claim
        return ModifierProvenance.objects.get(source_reference=f"theme_claim:{claim.pk}")

    def test_review_page_groups_pending_theme_claim_without_changing_decision_controls(self):
        response = self.client.get(reverse("submission-review", kwargs={
            "group_slug": self.group.slug,
            "month_pk": self.month.pk,
            "pk": self.submission.pk,
        }))
        self.assertContains(response, "Theme Claims")
        self.assertContains(response, self.theme.name)
        self.assertContains(response, "Save Review")

    def test_first_approval_and_repeat_use_one_current_snapshot(self):
        statuses = {self.claim.pk: ThemeClaim.Status.APPROVED}
        self.review(BookSubmission.Status.APPROVED, statuses)
        self.claim.refresh_from_db()
        source = self.source()
        self.assertEqual(self.claim.approved_bonus_pages, 25)
        self.assertEqual((source.amount, source.is_active), (25, True))
        self.assertEqual((self.submission.approved_pages, self.submission.bonus_pages, self.submission.final_scored_pages), (200, 25, 225))

        self.review(BookSubmission.Status.APPROVED, statuses)
        self.assertEqual(ModifierProvenance.objects.filter(source_reference=source.source_reference).count(), 1)
        self.assertEqual((self.submission.bonus_pages, self.submission.final_scored_pages), (25, 225))

    def test_rejection_and_reapproval_restore_historical_amount_after_theme_edit(self):
        self.review(BookSubmission.Status.APPROVED, {self.claim.pk: ThemeClaim.Status.APPROVED})
        source_pk = self.source().pk

        self.review(BookSubmission.Status.APPROVED, {self.claim.pk: ThemeClaim.Status.REJECTED})
        self.claim.refresh_from_db()
        source = self.source()
        self.assertEqual(self.claim.approved_bonus_pages, 0)
        self.assertEqual((source.pk, source.amount, source.is_active), (source_pk, 25, False))
        self.assertEqual((self.submission.bonus_pages, self.submission.final_scored_pages), (0, 200))

        self.theme.bonus_pages = 50
        self.theme.save(update_fields=["bonus_pages"])
        self.review(BookSubmission.Status.APPROVED, {self.claim.pk: ThemeClaim.Status.APPROVED})
        self.claim.refresh_from_db()
        source = self.source()
        self.assertEqual((self.claim.approved_bonus_pages, source.amount, source.is_active), (25, 25, True))
        self.assertEqual((self.submission.approved_pages, self.submission.bonus_pages, self.submission.final_scored_pages), (200, 25, 225))

    def test_never_approved_claim_uses_theme_amount_at_first_approval(self):
        self.review(BookSubmission.Status.APPROVED, {self.claim.pk: ThemeClaim.Status.REJECTED})
        self.assertFalse(ModifierProvenance.objects.filter(source_reference=f"theme_claim:{self.claim.pk}").exists())
        self.theme.bonus_pages = 50
        self.theme.save(update_fields=["bonus_pages"])

        self.review(BookSubmission.Status.APPROVED, {self.claim.pk: ThemeClaim.Status.APPROVED})
        self.claim.refresh_from_db()
        self.assertEqual((self.claim.approved_bonus_pages, self.source().amount), (50, 50))
        self.assertEqual((self.submission.bonus_pages, self.submission.final_scored_pages), (50, 250))

    def test_stacked_claims_are_independent_and_theme_edits_are_not_retroactive(self):
        second_theme = self.make_theme("Second Theme", 40)
        second_claim = ThemeClaim.objects.create(submission=self.submission, theme=second_theme)
        self.review(BookSubmission.Status.APPROVED, {
            self.claim.pk: ThemeClaim.Status.APPROVED,
            second_claim.pk: ThemeClaim.Status.APPROVED,
        })
        self.assertEqual((self.submission.bonus_pages, self.submission.final_scored_pages), (65, 265))

        self.theme.bonus_pages = 90
        self.theme.save(update_fields=["bonus_pages"])
        self.review(BookSubmission.Status.APPROVED, {
            self.claim.pk: ThemeClaim.Status.REJECTED,
            second_claim.pk: ThemeClaim.Status.APPROVED,
        })
        self.assertFalse(self.source().is_active)
        self.assertTrue(self.source(second_claim).is_active)
        self.assertEqual((self.submission.bonus_pages, self.submission.final_scored_pages), (40, 240))

    def test_parent_rejection_requires_explicit_claim_reapproval_and_preserves_history(self):
        self.review(BookSubmission.Status.APPROVED, {self.claim.pk: ThemeClaim.Status.APPROVED})
        source_pk = self.source().pk
        self.review(BookSubmission.Status.REJECTED, {self.claim.pk: ThemeClaim.Status.APPROVED})
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ThemeClaim.Status.REJECTED)
        self.assertEqual(self.claim.approved_bonus_pages, 0)
        self.assertEqual((self.source().pk, self.source().amount, self.source().is_active), (source_pk, 25, False))
        self.assertEqual((self.submission.approved_pages, self.submission.bonus_pages, self.submission.final_scored_pages), (200, 0, None))

        self.review(BookSubmission.Status.APPROVED, {self.claim.pk: ThemeClaim.Status.REJECTED})
        self.assertEqual((self.submission.bonus_pages, self.submission.final_scored_pages), (0, 200))
        self.review(BookSubmission.Status.APPROVED, {self.claim.pk: ThemeClaim.Status.APPROVED})
        self.assertEqual((self.source().amount, self.source().is_active), (25, True))
        self.assertEqual((self.submission.bonus_pages, self.submission.final_scored_pages), (25, 225))

    def test_removed_submission_and_checkpoint_history_survive_review_transitions(self):
        self.review(BookSubmission.Status.APPROVED, {self.claim.pk: ThemeClaim.Status.APPROVED})
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
            participant=self.reader,
            evaluated_at=timezone.now(),
            threshold_percentage=25,
            progress_basis=ProgressCheckpoint.ProgressBasis.TOTAL,
            target_basis=ProgressCheckpoint.TargetBasis.FIXED,
            target_pages=Decimal("1000"),
            required_pages=Decimal("250"),
            progress_pages=225,
            outcome=ProgressCheckpointResult.Outcome.BELOW,
        )
        self.submission.is_removed = True
        self.submission.save(update_fields=["is_removed"])
        self.review(BookSubmission.Status.APPROVED, {self.claim.pk: ThemeClaim.Status.REJECTED})

        result.refresh_from_db()
        self.assertTrue(self.submission.is_removed)
        self.assertTrue(ModifierProvenance.objects.filter(pk=self.source().pk).exists())
        self.assertEqual((result.progress_pages, result.outcome), (225, ProgressCheckpointResult.Outcome.BELOW))
