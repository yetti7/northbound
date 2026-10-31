from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .botm_matching import synchronize_submission as synchronize_botm_submission
from .models import (
    BookSubmission, BotmBook, ChallengeMonth, ChallengeStaffAssignment, Membership,
    ModifierProvenance, MonthEnrollment, MonthTheme, PersonalTBR, PersonalTBRBook,
    PersonalTBRMatch, ReadingGroup, ThemeClaim,
)
from .participation import activate_participation, deactivate_participation
from .personal_tbr_matching import synchronize_reader, synchronize_submission
from .personal_tbr_rewards import (
    personal_tbr_book_reward_source_reference, synchronize_personal_tbr_book_reward,
    synchronize_personal_tbr_book_rewards_for_challenge,
    synchronize_personal_tbr_book_rewards_for_reader,
    synchronize_personal_tbr_book_rewards_for_submission,
)
from .score_aggregation import challenge_score_totals
from .scoring import refresh_submission_score


class PersonalTbrPerBookRewardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.host_user = User.objects.create_user("tbr-reward-host")
        self.reader_user = User.objects.create_user("tbr-reward-reader")
        self.group = ReadingGroup.objects.create(name="TBR Rewards", slug="tbr-rewards")
        self.host = Membership.objects.create(group=self.group, user=self.host_user, display_name="Host")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Reader")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="TBR Reward Challenge", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.ACTIVE,
            tbr_enabled=True, tbr_book_bonus_pages=50,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST,
        )
        self.enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        self.tbr = PersonalTBR.objects.create(enrollment=self.enrollment)

    def add_tbr_book(self, position=1, title="Reward Book", author="Reward Author"):
        return PersonalTBRBook.objects.create(
            personal_tbr=self.tbr, position=position, title_snapshot=title, author_snapshot=author,
        )

    def lock_tbr(self):
        self.tbr.confirmed_at = timezone.now()
        self.tbr.save(update_fields=["confirmed_at"])

    def submission(self, *, title="Reward Book", author="Reward Author", completed_on=date(2026, 8, 15),
                   status=BookSubmission.Status.APPROVED, removed=False):
        return BookSubmission.objects.create(
            month=self.month, participant=self.reader, title=title, author=author,
            book_format=BookSubmission.Format.EBOOK, completed_on=completed_on,
            submitted_pages=300, approved_pages=300 if status == BookSubmission.Status.APPROVED else None,
            status=status, is_removed=removed,
        )

    def qualify(self, **submission_values):
        if not self.tbr.books.exists():
            self.add_tbr_book()
        if self.tbr.confirmed_at is None:
            self.lock_tbr()
        submission = self.submission(**submission_values)
        synchronize_submission(submission)
        return submission, PersonalTBRMatch.objects.get(submission=submission)

    def provenance(self, match=None):
        query = ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_BOOK)
        if match is not None:
            query = query.filter(source_reference=personal_tbr_book_reward_source_reference(match))
        return query.get()

    def score(self, **bounds):
        return challenge_score_totals(month=self.month, participant_ids=[self.reader.pk], **bounds)[self.reader.pk]

    def test_qualifying_match_creates_stable_submission_linked_frozen_reward_once(self):
        submission, match = self.qualify()
        synchronize_submission(submission)
        synchronize_personal_tbr_book_reward(match)
        provenance = self.provenance(match)
        self.assertEqual(provenance.source_reference, f"personal_tbr_match:{match.pk}")
        self.assertEqual((provenance.month, provenance.participant, provenance.submission), (self.month, self.reader, submission))
        self.assertEqual((provenance.amount, provenance.effective_date, provenance.is_active), (50, submission.completed_on, True))
        self.assertTrue(provenance.is_system_generated)
        self.assertEqual(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_BOOK).count(), 1)
        submission.refresh_from_db()
        self.assertEqual((submission.approved_pages, submission.bonus_pages, submission.final_scored_pages), (300, 50, 350))
        self.assertEqual(self.score(), {"base_pages": 300, "modifier_pages": 50, "total_pages": 350})

    def test_amount_freezes_while_new_book_uses_later_configuration(self):
        self.add_tbr_book()
        self.add_tbr_book(position=2, title="Later Book", author="Later Author")
        self.lock_tbr()
        first_submission, first_match = self.qualify()
        first_provenance = self.provenance(first_match)
        self.month.tbr_book_bonus_pages = 75; self.month.save(update_fields=["tbr_book_bonus_pages"])
        synchronize_personal_tbr_book_reward(first_match)
        first_provenance.refresh_from_db(); self.assertEqual(first_provenance.amount, 50)
        later_submission = self.submission(title="Later Book", author="Later Author")
        synchronize_submission(later_submission)
        amounts = list(ModifierProvenance.objects.filter(
            source_type=ModifierProvenance.SourceType.TBR_BOOK,
        ).order_by("pk").values_list("amount", flat=True))
        self.assertEqual(amounts, [50, 75])
        self.assertEqual(first_submission.approved_pages, 300)

    def test_zero_bonus_creates_nothing_then_positive_setting_establishes_reward(self):
        self.month.tbr_book_bonus_pages = 0; self.month.save(update_fields=["tbr_book_bonus_pages"])
        submission, match = self.qualify()
        self.assertTrue(match.is_qualifying)
        self.assertFalse(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_BOOK).exists())
        self.month.tbr_book_bonus_pages = 40; self.month.save(update_fields=["tbr_book_bonus_pages"])
        synchronize_personal_tbr_book_reward(match)
        self.assertEqual(self.provenance(match).amount, 40)
        self.assertEqual(self.score(), {"base_pages": 300, "modifier_pages": 40, "total_pages": 340})

    def test_pending_rejected_confirmed_and_reconfirmed_toggle_same_reward(self):
        book = self.add_tbr_book(); self.lock_tbr(); submission = self.submission()
        match = PersonalTBRMatch.objects.create(
            personal_tbr_book=book, month=self.month, participant=self.reader, submission=submission,
            method=PersonalTBRMatch.Method.NORMALIZED_TITLE_AUTHOR,
            status=PersonalTBRMatch.Status.PENDING_REVIEW, is_qualifying=False,
            tbr_title_snapshot=book.title_snapshot, tbr_author_snapshot=book.author_snapshot,
            submission_title_snapshot=submission.title, submission_author_snapshot=submission.author,
        )
        synchronize_personal_tbr_book_reward(match)
        self.assertFalse(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_BOOK).exists())
        match.status = PersonalTBRMatch.Status.REJECTED; match.save(update_fields=["status", "updated_at"])
        synchronize_personal_tbr_book_reward(match)
        self.assertFalse(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_BOOK).exists())
        match.status = PersonalTBRMatch.Status.CONFIRMED; match.is_qualifying = True
        match.save(update_fields=["status", "is_qualifying", "updated_at"])
        synchronize_personal_tbr_book_reward(match); provenance = self.provenance(match)
        match.status = PersonalTBRMatch.Status.REJECTED; match.is_qualifying = False
        match.save(update_fields=["status", "is_qualifying", "updated_at"])
        synchronize_personal_tbr_book_reward(match); provenance.refresh_from_db(); self.assertFalse(provenance.is_active)
        match.status = PersonalTBRMatch.Status.CONFIRMED; match.is_qualifying = True
        match.save(update_fields=["status", "is_qualifying", "updated_at"])
        synchronize_personal_tbr_book_reward(match)
        self.assertEqual(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_BOOK).count(), 1)
        provenance.refresh_from_db(); self.assertTrue(provenance.is_active)

    def test_rejection_removal_restore_and_reapproval_reuse_history(self):
        submission, match = self.qualify(); provenance = self.provenance(match)
        submission.status = BookSubmission.Status.REJECTED; submission.approved_pages = None; submission.save(); synchronize_submission(submission)
        provenance.refresh_from_db(); self.assertFalse(provenance.is_active)
        submission.status = BookSubmission.Status.APPROVED; submission.approved_pages = 300; submission.save(); synchronize_submission(submission)
        provenance.refresh_from_db(); self.assertTrue(provenance.is_active)
        submission.is_removed = True; submission.save(update_fields=["is_removed"]); synchronize_submission(submission)
        provenance.refresh_from_db(); self.assertFalse(provenance.is_active)
        submission.is_removed = False; submission.save(update_fields=["is_removed"]); synchronize_submission(submission)
        provenance.refresh_from_db(); self.assertTrue(provenance.is_active)
        self.assertEqual(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_BOOK).count(), 1)

    def test_withdrawal_and_reactivation_reuse_same_provenance(self):
        _, match = self.qualify(); provenance = self.provenance(match)
        deactivate_participation(
            enrollment=self.enrollment, actor=self.reader_user,
            reason=MonthEnrollment.InactiveReason.WITHDRAWN,
        )
        provenance.refresh_from_db(); self.assertFalse(provenance.is_active)
        activate_participation(
            month=self.month, participant=self.reader, actor=self.reader_user,
            origin=MonthEnrollment.Origin.SELF,
        )
        provenance.refresh_from_db(); self.assertTrue(provenance.is_active)
        self.assertEqual(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_BOOK).count(), 1)

    def test_feature_and_zero_bonus_toggle_deactivate_without_deleting_then_reactivate(self):
        _, match = self.qualify(); provenance = self.provenance(match)
        self.month.tbr_enabled = False; self.month.save(update_fields=["tbr_enabled"])
        synchronize_personal_tbr_book_rewards_for_challenge(self.month)
        provenance.refresh_from_db(); self.assertFalse(provenance.is_active)
        self.month.tbr_enabled = True; self.month.save(update_fields=["tbr_enabled"])
        synchronize_personal_tbr_book_rewards_for_challenge(self.month)
        provenance.refresh_from_db(); self.assertTrue(provenance.is_active)
        self.month.tbr_book_bonus_pages = 0; self.month.save(update_fields=["tbr_book_bonus_pages"])
        synchronize_personal_tbr_book_rewards_for_challenge(self.month)
        provenance.refresh_from_db(); self.assertFalse(provenance.is_active)
        self.month.tbr_book_bonus_pages = 75; self.month.save(update_fields=["tbr_book_bonus_pages"])
        synchronize_personal_tbr_book_rewards_for_challenge(self.month)
        provenance.refresh_from_db(); self.assertTrue(provenance.is_active); self.assertEqual(provenance.amount, 50)

    def test_multiple_historical_submissions_leave_one_active_reward_for_book(self):
        self.add_tbr_book(); self.lock_tbr()
        first = self.submission(); second = self.submission()
        synchronize_reader(month=self.month, participant=self.reader)
        self.assertEqual(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_BOOK, is_active=True).count(), 1)
        first.is_removed = True; first.save(update_fields=["is_removed"])
        synchronize_reader(month=self.month, participant=self.reader)
        active = ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_BOOK, is_active=True)
        self.assertEqual(active.count(), 1); self.assertEqual(active.get().submission, second)

    def test_theme_botm_tbr_and_game_stack_without_double_counting(self):
        self.add_tbr_book(); self.lock_tbr(); submission = self.submission()
        theme = MonthTheme.objects.create(bonus_rule="legacy_fixed",
            month=self.month, name="Theme", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), bonus_pages=25,
        )
        ThemeClaim.objects.create(
            submission=submission, theme=theme, status=ThemeClaim.Status.APPROVED,
            approved_bonus_pages=25, reviewed_by=self.host_user, reviewed_at=timezone.now(),
        )
        self.month.botm_enabled = True; self.month.save(update_fields=["botm_enabled"])
        BotmBook.objects.create(
            month=self.month, position=1, title_snapshot="Reward Book", author_snapshot="Reward Author",
            page_count_snapshot=300, bonus_pages=60,
        )
        synchronize_submission(submission)
        synchronize_botm_submission(submission)
        ModifierProvenance.objects.create(
            month=self.month, participant=self.reader, source_type=ModifierProvenance.SourceType.GAME_REWARD,
            source_reference="game_reward_recipient:tbr-stack", source_label="Game", amount=40,
            effective_date=date(2026, 8, 16), is_active=True,
        )
        refresh_submission_score(submission); submission.refresh_from_db()
        self.assertEqual((submission.approved_pages, submission.bonus_pages, submission.final_scored_pages), (300, 135, 435))
        self.assertEqual(self.score(), {"base_pages": 300, "modifier_pages": 175, "total_pages": 475})

    def test_date_range_uses_completion_not_review_date_and_batch_apis_are_idempotent(self):
        submission, match = self.qualify(completed_on=date(2026, 8, 12))
        match.decided_at = timezone.now(); match.save(update_fields=["decided_at", "updated_at"])
        synchronize_personal_tbr_book_reward(match)
        synchronize_personal_tbr_book_rewards_for_submission(submission)
        synchronize_personal_tbr_book_rewards_for_reader(month=self.month, participant=self.reader)
        synchronize_personal_tbr_book_rewards_for_challenge(self.month)
        self.assertEqual(self.score(effective_from=date(2026, 8, 10), effective_to=date(2026, 8, 15))["modifier_pages"], 50)
        self.assertEqual(self.score(effective_from=date(2026, 8, 13), effective_to=date(2026, 8, 31)), {"base_pages": 0, "modifier_pages": 0, "total_pages": 0})
        self.assertEqual(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_BOOK).count(), 1)
        self.assertFalse(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.TBR_COMPLETION).exists())
