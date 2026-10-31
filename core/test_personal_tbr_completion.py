from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .botm_matching import synchronize_submission as synchronize_botm_submission
from .models import (
    BookSubmission, BotmBook, ChallengeMonth, ChallengeStaffAssignment, Membership,
    ModifierProvenance, MonthEnrollment, MonthTheme, PersonalTBR, PersonalTBRBook,
    PersonalTBRCompletionAward, PersonalTBRMatch, ProgressCheckpoint, ReadingGroup,
    ThemeClaim,
)
from .participation import activate_participation, deactivate_participation
from .personal_tbr_completion import (
    personal_tbr_completion_set_fingerprint, personal_tbr_completion_source_reference,
    synchronize_personal_tbr_completion_for_reader,
    synchronize_personal_tbr_completions_for_challenge,
)
from .personal_tbr_matching import synchronize_submission
from .progress_checkpoints import process_due_progress_checkpoints
from .score_aggregation import challenge_score_totals
from .scoring import refresh_submission_score


class PersonalTbrCompletionRewardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.host_user = User.objects.create_user("tbr-completion-host")
        self.reader_user = User.objects.create_user("tbr-completion-reader")
        self.group = ReadingGroup.objects.create(name="TBR Completion", slug="tbr-completion")
        self.host = Membership.objects.create(group=self.group, user=self.host_user, display_name="Host")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Reader")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="TBR Completion Challenge", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.ACTIVE,
            tbr_enabled=True, tbr_book_bonus_pages=50, tbr_completion_bonus_pages=1000,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST,
        )
        self.enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader)

    def locked_tbr(self, count=9):
        tbr = PersonalTBR.objects.create(enrollment=self.enrollment)
        books = [PersonalTBRBook.objects.create(
            personal_tbr=tbr, position=position, title_snapshot=f"TBR {position}",
            author_snapshot=f"Author {position}",
        ) for position in range(1, count + 1)]
        tbr.confirmed_at = timezone.now(); tbr.save(update_fields=["confirmed_at"])
        return tbr, books

    def submit_for(self, book):
        submission = BookSubmission.objects.create(
            month=self.month, participant=self.reader, title=book.title_snapshot,
            author=book.author_snapshot, book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 8, min(28, book.position + 1)), submitted_pages=100,
            approved_pages=100, status=BookSubmission.Status.APPROVED,
        )
        synchronize_submission(submission)
        return submission

    def qualify(self, count=9):
        tbr, books = self.locked_tbr(count)
        submissions = [self.submit_for(book) for book in books]
        return tbr, books, submissions

    def completion_provenance(self):
        return ModifierProvenance.objects.get(source_type=ModifierProvenance.SourceType.TBR_COMPLETION)

    def score(self, **bounds):
        return challenge_score_totals(month=self.month, participant_ids=[self.reader.pk], **bounds)[self.reader.pk]

    def test_partial_tbr_and_eight_of_nine_never_complete(self):
        self.qualify(8)
        self.assertFalse(PersonalTBRCompletionAward.objects.exists())

        # A separate Reader with exactly nine locked books but only eight matches also cannot complete.
        other_user = get_user_model().objects.create_user("tbr-eight-of-nine")
        other = Membership.objects.create(group=self.group, user=other_user, display_name="Eight")
        enrollment = MonthEnrollment.objects.create(month=self.month, participant=other)
        tbr = PersonalTBR.objects.create(enrollment=enrollment)
        books = [PersonalTBRBook.objects.create(
            personal_tbr=tbr, position=position, title_snapshot=f"Other {position}", author_snapshot=f"Writer {position}",
        ) for position in range(1, 10)]
        tbr.confirmed_at = timezone.now(); tbr.save(update_fields=["confirmed_at"])
        for book in books[:8]:
            submission = BookSubmission.objects.create(
                month=self.month, participant=other, title=book.title_snapshot, author=book.author_snapshot,
                book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 10),
                submitted_pages=100, approved_pages=100, status=BookSubmission.Status.APPROVED,
            )
            synchronize_submission(submission)
        self.assertFalse(PersonalTBRCompletionAward.objects.filter(participant=other).exists())

    def test_exact_nine_freezes_set_and_creates_standalone_stable_provenance(self):
        before = timezone.now(); tbr, books, submissions = self.qualify(); after = timezone.now()
        award = PersonalTBRCompletionAward.objects.get(); provenance = self.completion_provenance()
        self.assertEqual(award.personal_tbr, tbr)
        self.assertEqual(award.completion_set_fingerprint, personal_tbr_completion_set_fingerprint(books))
        self.assertEqual((award.configured_book_count, award.configured_books.count()), (9, 9))
        self.assertEqual(set(award.configured_books.values_list("personal_tbr_book_id", flat=True)), {book.pk for book in books})
        self.assertEqual(award.bonus_amount_snapshot, 1000)
        self.assertLessEqual(before, award.qualified_at); self.assertLessEqual(award.qualified_at, after)
        self.assertEqual(provenance.source_reference, personal_tbr_completion_source_reference(award))
        self.assertEqual((provenance.submission, provenance.amount, provenance.effective_date), (None, 1000, award.effective_date))
        synchronize_personal_tbr_completion_for_reader(month=self.month, participant=self.reader)
        self.assertEqual((PersonalTBRCompletionAward.objects.count(), ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_COMPLETION).count()), (1, 1))
        self.assertEqual(sum(submission.approved_pages for submission in submissions), 900)

    def test_amount_and_effective_date_freeze_across_setting_change_and_reactivation(self):
        _, _, submissions = self.qualify(); award = PersonalTBRCompletionAward.objects.get(); provenance = self.completion_provenance()
        original = (award.bonus_amount_snapshot, award.effective_date, award.qualified_at, provenance.amount)
        self.month.tbr_completion_bonus_pages = 1500; self.month.save(update_fields=["tbr_completion_bonus_pages"])
        submissions[0].status = BookSubmission.Status.REJECTED; submissions[0].approved_pages = None
        submissions[0].save(); synchronize_submission(submissions[0])
        submissions[0].status = BookSubmission.Status.APPROVED; submissions[0].approved_pages = 100
        submissions[0].save(); synchronize_submission(submissions[0])
        award.refresh_from_db(); provenance.refresh_from_db()
        self.assertEqual((award.bonus_amount_snapshot, award.effective_date, award.qualified_at, provenance.amount), original)
        self.assertTrue(award.is_qualifying); self.assertTrue(provenance.is_active)

    def test_rejection_removal_and_match_decision_churn_reuse_award_and_provenance(self):
        _, _, submissions = self.qualify(); award = PersonalTBRCompletionAward.objects.get(); provenance = self.completion_provenance()
        ids = (award.pk, provenance.pk)
        submission = submissions[0]
        submission.status = BookSubmission.Status.REJECTED; submission.approved_pages = None; submission.save(); synchronize_submission(submission)
        award.refresh_from_db(); provenance.refresh_from_db(); self.assertFalse(award.is_qualifying); self.assertFalse(provenance.is_active)
        submission.status = BookSubmission.Status.APPROVED; submission.approved_pages = 100; submission.save(); synchronize_submission(submission)
        submission.is_removed = True; submission.save(update_fields=["is_removed"]); synchronize_submission(submission)
        award.refresh_from_db(); self.assertFalse(award.is_qualifying)
        submission.is_removed = False; submission.save(update_fields=["is_removed"]); synchronize_submission(submission)
        match = PersonalTBRMatch.objects.get(submission=submission)
        match.status = PersonalTBRMatch.Status.REJECTED; match.is_qualifying = False
        match.save(update_fields=["status", "is_qualifying", "updated_at"])
        synchronize_personal_tbr_completion_for_reader(month=self.month, participant=self.reader)
        award.refresh_from_db(); self.assertFalse(award.is_qualifying)
        match.status = PersonalTBRMatch.Status.CONFIRMED; match.is_qualifying = True
        match.save(update_fields=["status", "is_qualifying", "updated_at"])
        synchronize_personal_tbr_completion_for_reader(month=self.month, participant=self.reader)
        award.refresh_from_db(); provenance.refresh_from_db()
        self.assertEqual((award.pk, provenance.pk), ids); self.assertTrue(award.is_qualifying); self.assertTrue(provenance.is_active)
        self.assertEqual(award.configured_books.count(), 9)

    def test_withdrawal_and_feature_toggle_reactivate_same_history(self):
        self.qualify(); award = PersonalTBRCompletionAward.objects.get(); provenance = self.completion_provenance()
        deactivate_participation(enrollment=self.enrollment, actor=self.reader_user, reason=MonthEnrollment.InactiveReason.WITHDRAWN)
        award.refresh_from_db(); provenance.refresh_from_db(); self.assertFalse(award.is_qualifying); self.assertFalse(provenance.is_active)
        activate_participation(month=self.month, participant=self.reader, actor=self.reader_user, origin=MonthEnrollment.Origin.SELF)
        award.refresh_from_db(); self.assertTrue(award.is_qualifying)
        self.month.tbr_enabled = False; self.month.save(update_fields=["tbr_enabled"])
        synchronize_personal_tbr_completions_for_challenge(self.month)
        provenance.refresh_from_db(); self.assertFalse(provenance.is_active)
        self.month.tbr_enabled = True; self.month.save(update_fields=["tbr_enabled"])
        synchronize_personal_tbr_completions_for_challenge(self.month)
        award.refresh_from_db(); provenance.refresh_from_db(); self.assertTrue(award.is_qualifying); self.assertTrue(provenance.is_active)
        self.assertEqual((PersonalTBRCompletionAward.objects.count(), award.configured_books.count()), (1, 9))

    def test_zero_bonus_preserves_logical_completion_without_zero_provenance(self):
        self.month.tbr_completion_bonus_pages = 0; self.month.save(update_fields=["tbr_completion_bonus_pages"])
        self.qualify(); award = PersonalTBRCompletionAward.objects.get()
        self.assertTrue(award.is_qualifying); self.assertEqual(award.bonus_amount_snapshot, 0)
        self.assertFalse(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_COMPLETION).exists())

    def test_all_per_book_completion_theme_botm_and_game_modifiers_stack_once(self):
        _, books, submissions = self.qualify()
        first = submissions[0]
        theme = MonthTheme.objects.create(bonus_rule="legacy_fixed",
            month=self.month, name="Theme", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), bonus_pages=25,
        )
        ThemeClaim.objects.create(
            submission=first, theme=theme, status=ThemeClaim.Status.APPROVED,
            approved_bonus_pages=25, reviewed_by=self.host_user, reviewed_at=timezone.now(),
        )
        self.month.botm_enabled = True; self.month.save(update_fields=["botm_enabled"])
        BotmBook.objects.create(
            month=self.month, position=1, title_snapshot=books[0].title_snapshot,
            author_snapshot=books[0].author_snapshot, page_count_snapshot=100, bonus_pages=60,
        )
        synchronize_botm_submission(first)
        ModifierProvenance.objects.create(
            month=self.month, participant=self.reader, source_type=ModifierProvenance.SourceType.GAME_REWARD,
            source_reference="game_reward_recipient:tbr-completion-stack", source_label="Game",
            amount=40, effective_date=date(2026, 8, 20), is_active=True,
        )
        refresh_submission_score(first); first.refresh_from_db()
        self.assertEqual((first.approved_pages, first.bonus_pages, first.final_scored_pages), (100, 135, 235))
        self.assertEqual(self.score(), {"base_pages": 900, "modifier_pages": 1575, "total_pages": 2475})

    def test_frozen_effective_date_controls_ranges_and_checkpoint_history_is_immutable(self):
        tbr, books = self.locked_tbr()
        submissions = []
        for book in books:
            submission = BookSubmission.objects.create(
                month=self.month, participant=self.reader, title=book.title_snapshot, author=book.author_snapshot,
                book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 10),
                submitted_pages=100, approved_pages=100, status=BookSubmission.Status.APPROVED,
            )
            submissions.append(submission)
            PersonalTBRMatch.objects.create(
                personal_tbr_book=book, month=self.month, participant=self.reader, submission=submission,
                method=PersonalTBRMatch.Method.NORMALIZED_TITLE_AUTHOR,
                status=PersonalTBRMatch.Status.CONFIRMED, is_qualifying=True,
                tbr_title_snapshot=book.title_snapshot, tbr_author_snapshot=book.author_snapshot,
                submission_title_snapshot=submission.title, submission_author_snapshot=submission.author,
            )
        completed_at = datetime(2026, 8, 20, 15, tzinfo=ZoneInfo("America/New_York"))
        award = synchronize_personal_tbr_completion_for_reader(month=self.month, participant=self.reader, now=completed_at)
        before = self.score(effective_to=date(2026, 8, 19)); including = self.score(effective_to=date(2026, 8, 20))
        self.assertEqual((before["modifier_pages"], including["modifier_pages"], award.effective_date), (0, 1000, date(2026, 8, 20)))
        checkpoint = ProgressCheckpoint.objects.create(
            month=self.month, scheduled_at=completed_at, threshold_percentage=50,
            progress_basis=ProgressCheckpoint.ProgressBasis.TOTAL,
            target_basis=ProgressCheckpoint.TargetBasis.FIXED, fixed_target_pages=1900,
        )
        process_due_progress_checkpoints(now=completed_at + timedelta(minutes=1))
        result = checkpoint.results.get(participant=self.reader); self.assertEqual(result.progress_pages, 1900)
        submissions[0].is_removed = True; submissions[0].save(update_fields=["is_removed"])
        synchronize_submission(submissions[0])
        result.refresh_from_db(); self.assertEqual(result.progress_pages, 1900)
