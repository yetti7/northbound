from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import test_late_approval as fixture
from .models import (
    AuditEvent, BookSubmission, BotmBook, BotmCompletionAward, BotmMatch, ChallengeMonth,
    ImmersiveRequest, ModifierProvenance, MonthEnrollment, MonthTheme,
    PersonalTBRCompletionAward, PersonalTBRMatch, RecoveryOperation, TeamAssignment,
)
from .participation import deactivate_participation
from .recovery import RecoveryRequest
from .recovery_scoring import (
    preview_submission_restoration, restore_submission_as_staff, set_submission_removed,
    staff_restore_confirmation, submission_recovery_label,
)
from .score_aggregation import challenge_score_totals, challenge_team_totals


class HistoricalRestorationTests(TestCase):
    setUp = fixture.LateApprovalTests.setUp
    activate = fixture.LateApprovalTests.activate
    assign = fixture.LateApprovalTests.assign
    depart = fixture.LateApprovalTests.depart
    submit = fixture.LateApprovalTests.submit
    approve = fixture.LateApprovalTests.approve
    configure_awards = fixture.LateApprovalTests.configure_awards

    def void(self, book):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("submission-remove", args=[self.group.slug, self.month.pk, book.pk]),
                                    {"reason": "Void for test"})
        self.assertEqual(response.status_code, 302)
        book.refresh_from_db()
        self.assertTrue(book.void_earning_evidence)
        return book

    def restore(self, book, *, token=None, actor=None):
        return restore_submission_as_staff(submission=book, actor=actor or self.owner,
            confirmation=token or staff_restore_confirmation(book, actor or self.owner))

    def request(self, book):
        label = submission_recovery_label(book)
        return RecoveryRequest(actor=self.owner, tier=2, action="submission.restore",
            target_type="BookSubmission", target_id=str(book.pk), target_label=label, reason="Verify historical credit",
            required_confirmation=label, supplied_confirmation=label, group=self.group, challenge=self.month)

    def test_withdrawal_removal_and_checkpoint_removal_preserve_enrollment(self):
        for reason in ("withdrawal", "staff", "checkpoint"):
            with self.subTest(reason=reason):
                if not self.enrollment.is_active:
                    # Start a fresh local day to avoid two-period date ambiguity.
                    self.enrollment = self.activate()
                    self.assign(self.team)
                    self.enrollment.participation_periods.filter(ended_at__isnull=True).update(started_at=timezone.now() - timedelta(days=1))
                self.client.force_login(self.user)
                book = self.approve(self.submit(title=reason))
                self.void(book)
                self.enrollment = deactivate_participation(
                    enrollment=self.enrollment, actor=self.user if reason == "withdrawal" else self.owner,
                    reason="withdrawn" if reason == "withdrawal" else "removed",
                    note="checkpoint evaluation" if reason == "checkpoint" else "",
                    ordinary_staff_removal=reason != "withdrawal",
                )[0]
                before = list(MonthEnrollment.objects.values())
                periods = list(self.enrollment.participation_periods.values())
                self.restore(book)
                book.refresh_from_db()
                self.assertEqual(book.final_scored_pages, 400)
                self.assertEqual(list(MonthEnrollment.objects.values()), before)
                self.assertEqual(list(self.enrollment.participation_periods.values()), periods)

    def test_transfer_and_reactivation_follow_current_or_last_credited_team(self):
        book = self.approve(self.submit())
        self.void(book)
        self.assign(self.other_team)
        self.depart()
        self.enrollment = self.activate()
        assignments = list(TeamAssignment.objects.values())
        self.restore(book)
        self.assertEqual(challenge_team_totals(month=self.month, team_ids=[self.team.pk, self.other_team.pk]),
                         {self.team.pk: 0, self.other_team.pk: 400})
        self.assertEqual(list(TeamAssignment.objects.values()), assignments)
        self.void(book)
        self.assign(self.team)
        self.restore(book)
        self.assertEqual(challenge_team_totals(month=self.month, team_ids=[self.team.pk, self.other_team.pk]),
                         {self.team.pk: 400, self.other_team.pk: 0})

    def test_missing_team_and_edited_or_outside_period_evidence_fail_closed(self):
        book = self.approve(self.submit())
        self.void(book)
        self.depart()
        self.enrollment.credited_team = None
        self.enrollment.save(update_fields=["credited_team"])
        with self.assertRaises(ValidationError):
            self.restore(book)
        self.enrollment.credited_team = self.team
        self.enrollment.save(update_fields=["credited_team"])
        book.title = "Changed later"
        book.save(update_fields=["title"])
        with self.assertRaises(ValidationError):
            self.restore(book)
        book.title = "Book"
        book.completed_on -= timedelta(days=1)
        book.save(update_fields=["title", "completed_on"])
        with self.assertRaises(ValidationError):
            self.restore(book)

    def test_legacy_or_missing_void_receipt_remains_restricted_for_departed_reader(self):
        book = self.approve(self.submit())
        self.void(book)
        BookSubmission.objects.filter(pk=book.pk).update(void_earning_evidence={})
        book.refresh_from_db()
        self.depart()
        with self.assertRaises(ValidationError):
            self.restore(book)

    def test_stale_signed_confirmation_duplicate_and_transaction_rollback(self):
        book = self.approve(self.submit())
        self.void(book)
        token = staff_restore_confirmation(book, self.owner)
        self.depart()
        with self.assertRaises(ValidationError):
            self.restore(book, token=token)
        before = list(BookSubmission.objects.values())
        with patch("core.historical_restoration.apply_restoration_plan", side_effect=RuntimeError("rollback")):
            with self.assertRaises(RuntimeError):
                self.restore(book)
        self.assertEqual(list(BookSubmission.objects.values()), before)
        self.restore(book)
        with self.assertRaises(ValidationError):
            self.restore(book)

    def award_book(self):
        botm, tbr = self.configure_awards()
        from .personal_tbr_matching import synchronize_submission
        for position in range(1, 9):
            earlier = BookSubmission.objects.create(month=self.month, participant=self.reader,
                title=f"Earlier {position}", author="Author", book_format="ebook",
                completed_on=self.today, submitted_pages=10, approved_pages=10, status="approved")
            synchronize_submission(earlier)
        theme = MonthTheme.objects.create(month=self.month, name="Theme", bonus_percentage=10,
                                          starts_on=self.today, ends_on=self.today)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(immersive_enabled=True, immersive_bonus_percent=50)
        self.client.force_login(self.user)
        book = self.approve(self.submit(themes=[theme.pk], immersive_request="on"), themes=True)
        from .immersive import review_immersive
        review_immersive(request=book.immersive_requests.get(), actor=self.owner, decision="approved")
        book.refresh_from_db()
        return book, botm, tbr

    def test_frozen_earned_theme_immersive_and_completion_restore_without_unrelated_changes(self):
        book, botm, tbr = self.award_book()
        earned = list(ModifierProvenance.objects.order_by("pk").values())
        other_books = list(BookSubmission.objects.exclude(pk=book.pk).values())
        self.void(book)
        self.depart()
        ChallengeMonth.objects.filter(pk=self.month.pk).update(botm_completion_bonus_pages=999,
            tbr_book_bonus_pages=999, tbr_completion_bonus_pages=999, immersive_bonus_percent=200)
        self.restore(book)
        book.refresh_from_db()
        self.assertEqual(book.final_scored_pages, 680)
        self.assertEqual(list(ModifierProvenance.objects.order_by("pk").values()), earned)
        self.assertEqual(list(BookSubmission.objects.exclude(pk=book.pk).values()), other_books)
        self.assertTrue(BotmCompletionAward.objects.get().is_qualifying)
        self.assertTrue(PersonalTBRCompletionAward.objects.get(personal_tbr=tbr).is_qualifying)
        self.assertTrue(book.immersive_requests.get().is_active)

    def test_explicit_reversal_and_displaced_matches_stay_held(self):
        book, botm, tbr = self.award_book()
        self.void(book)
        source = book.modifier_provenance.get(source_type="theme_bonus")
        ModifierProvenance.objects.filter(pk=source.pk).update(voided_at=timezone.now(), voided_by=self.owner)
        BotmMatch.objects.filter(submission=book).update(status="rejected")
        ImmersiveRequest.objects.filter(submission=book).update(status="revoked", reviewed_at=timezone.now())
        other = BookSubmission.objects.create(month=self.month, participant=self.reader, title="Other",
            author="Author", book_format="ebook", completed_on=self.today, submitted_pages=10,
            approved_pages=10, status="approved")
        match = PersonalTBRMatch.objects.get(submission=book)
        PersonalTBRMatch.objects.create(month=self.month, participant=self.reader, submission=other,
            personal_tbr_book=match.personal_tbr_book, status="confirmed", is_qualifying=True, method="manual_review",
            tbr_title_snapshot=book.title, tbr_author_snapshot=book.author,
            submission_title_snapshot=other.title, submission_author_snapshot=other.author)
        self.depart()
        result = self.restore(book)
        self.assertTrue(result["held_credit"])
        self.assertFalse(book.modifier_provenance.filter(is_active=True).exists())
        self.assertFalse(BotmCompletionAward.objects.get().is_qualifying)
        self.assertFalse(PersonalTBRCompletionAward.objects.get(personal_tbr=tbr).is_qualifying)
        self.assertTrue(PersonalTBRMatch.objects.get(submission=other).is_qualifying)

    def test_unearned_claim_added_while_voided_cannot_award(self):
        book = self.approve(self.submit())
        self.void(book)
        theme = MonthTheme.objects.create(month=self.month, name="Unearned", bonus_percentage=100,
                                          starts_on=self.today, ends_on=self.today)
        from .models import ThemeClaim
        claim = ThemeClaim.objects.create(submission=book, theme=theme, status="approved")
        self.depart()
        self.restore(book)
        book.refresh_from_db()
        self.assertEqual(book.final_scored_pages, 400)
        self.assertFalse(book.modifier_provenance.exists())
        claim.refresh_from_db()
        self.assertFalse(claim.award_finalized)

    def test_finalized_advanced_preview_is_signed_and_ledger_records_approved_deltas(self):
        book = self.approve(self.submit())
        self.void(book)
        self.depart()
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status="completed")
        book.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            self.restore(book)
        before = list(BookSubmission.objects.values())
        preview, token = preview_submission_restoration(submission=book, actor=self.owner)
        self.assertEqual(list(BookSubmission.objects.values()), before)
        self.assertEqual(preview["reader_score_after"]["total_pages"], 400)
        self.assertEqual(preview["team_score_after"][str(self.team.pk)], 400)
        request = self.request(book)
        with self.assertRaises(ValidationError):
            set_submission_removed(submission=book, removed=False, recovery_request=request)
        operation = set_submission_removed(submission=book, removed=False, recovery_request=self.request(book), preview_token=token)
        self.assertEqual(operation.after_state, preview)
        self.assertEqual(operation.impact, preview)
        self.assertEqual(operation.actor_id, self.owner.pk)

    def test_advanced_preview_staleness_and_exact_confirmation_are_enforced(self):
        book = self.approve(self.submit())
        self.void(book)
        preview, token = preview_submission_restoration(submission=book, actor=self.owner)
        with self.assertRaises(ValidationError):
            set_submission_removed(submission=book, removed=False,
                recovery_request=replace(self.request(book), supplied_confirmation="wrong"), preview_token=token)
        self.assign(self.other_team)
        with self.assertRaises(ValidationError):
            set_submission_removed(submission=book, removed=False,
                recovery_request=self.request(book), preview_token=token)
        book.refresh_from_db()
        self.assertTrue(book.is_removed)

    def test_advanced_page_renders_score_preview_and_requires_it(self):
        book = self.approve(self.submit())
        self.void(book)
        self.client.force_login(self.owner)
        url = reverse("recovery-submission-status", args=[book.pk])
        response = self.client.get(url)
        self.assertContains(response, "Restoration Score Preview")
        self.assertContains(response, 'name="score_preview"')
        self.assertEqual(self.client.post(url, {"reason": "Restore earned book",
            "confirmation": submission_recovery_label(book)}).status_code, 200)
        book.refresh_from_db()
        self.assertTrue(book.is_removed)

    def test_pending_status_restores_without_creating_points(self):
        book = self.submit()
        self.void(book)
        self.depart()
        self.restore(book)
        book.refresh_from_db()
        self.assertEqual(book.status, "pending")
        self.assertIsNone(book.final_scored_pages)
        self.assertFalse(book.modifier_provenance.exists())

    def test_checkpoint_snapshot_is_preserved_and_out_of_period_completion_is_denied(self):
        from .models import ProgressCheckpoint, ProgressCheckpointResult
        book = self.approve(self.submit())
        checkpoint = ProgressCheckpoint.objects.create(month=self.month, scheduled_at=timezone.now())
        result = ProgressCheckpointResult.objects.create(checkpoint=checkpoint, participant=self.reader,
            evaluated_at=timezone.now(), threshold_percentage=25, progress_basis="base",
            target_basis="fixed", target_pages=500, required_pages=125, progress_pages=0, outcome="below")
        self.void(book)
        self.depart(removed=True)
        before = list(ProgressCheckpointResult.objects.values())
        self.restore(book)
        self.assertEqual(list(ProgressCheckpointResult.objects.values()), before)
        self.void(book)
        book.participation_period.__class__.objects.filter(pk=book.participation_period_id).update(
            started_at=book.accepted_at + timedelta(microseconds=1))
        with self.assertRaises(ValidationError):
            self.restore(book)

    def test_retired_book_and_altered_fixed_set_hold_previously_earned_completion(self):
        book, botm, tbr = self.award_book()
        self.void(book)
        BotmBook.objects.filter(pk=botm.pk).update(is_retired=True)
        self.depart()
        result = self.restore(book)
        self.assertIn(f"BOTM match #{book.botm_matches.get().pk}", result["held_credit"])
        self.assertFalse(BotmCompletionAward.objects.get().is_qualifying)
        self.assertFalse(book.modifier_provenance.get(source_type="botm_book").is_active)

    def test_later_refresh_does_not_award_unearned_or_held_credit(self):
        book, botm, tbr = self.award_book()
        self.void(book)
        from .models import ThemeClaim
        unearned = MonthTheme.objects.create(month=self.month, name="Later Theme", bonus_percentage=100,
                                             starts_on=self.today, ends_on=self.today)
        ThemeClaim.objects.create(submission=book, theme=unearned, status="approved", approved_bonus_pages=400)
        ThemeClaim.objects.filter(submission=book).exclude(theme=unearned).update(status="rejected")
        self.depart()
        self.restore(book)
        from .scoring import refresh_submission_score
        from .botm_matching import synchronize_submission
        earned = list(book.modifier_provenance.order_by("pk").values())
        refresh_submission_score(book)
        synchronize_submission(book)
        self.assertEqual(list(book.modifier_provenance.order_by("pk").values()), earned)
        self.assertFalse(book.modifier_provenance.filter(source_reference__startswith="theme_claim:", is_active=True).exists())
