from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .botm_configuration import retire_botm_book
from .botm_matching import adjudicate_match, synchronize_submission
from .botm_rewards import (
    botm_book_reward_source_reference,
    synchronize_botm_book_reward,
    synchronize_botm_book_rewards_for_challenge,
    synchronize_botm_book_rewards_for_reader,
    synchronize_botm_book_rewards_for_submission,
)
from .models import (
    BookSubmission, BotmBook, BotmMatch, CatalogBook, CatalogEdition, ChallengeMonth,
    ChallengeStaffAssignment, Membership, ModifierProvenance, MonthEnrollment,
    MonthTheme, ReadingGroup, ThemeClaim,
)
from .score_aggregation import challenge_score_totals
from .scoring import apply_submission_review, refresh_submission_score


class BotmPerBookRewardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.host_user = User.objects.create_user("reward-host")
        self.reader_user = User.objects.create_user("reward-reader")
        self.group = ReadingGroup.objects.create(name="BOTM Rewards", slug="botm-rewards")
        self.host = Membership.objects.create(group=self.group, user=self.host_user, display_name="Host")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Reader")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="Reward Challenge", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.ACTIVE, botm_enabled=True,
        )
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST)
        MonthEnrollment.objects.create(month=self.month, participant=self.reader)

    def botm(self, *, position=1, title="Reward Book", author="Reward Author", bonus=50, catalog_book=None, catalog_edition=None):
        return BotmBook.objects.create(
            month=self.month, position=position, catalog_book=catalog_book, catalog_edition=catalog_edition,
            title_snapshot=title, author_snapshot=author, page_count_snapshot=300, bonus_pages=bonus,
        )

    def submission(self, *, title="Reward Book", author="Reward Author", status=BookSubmission.Status.APPROVED, removed=False):
        return BookSubmission.objects.create(
            month=self.month, participant=self.reader, title=title, author=author,
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 15),
            submitted_pages=300, approved_pages=300 if status == BookSubmission.Status.APPROVED else None,
            status=status, is_removed=removed,
        )

    def provenance(self):
        return ModifierProvenance.objects.get(source_type=ModifierProvenance.SourceType.BOTM_BOOK)

    def score(self, *, effective_from=None, effective_to=None):
        return challenge_score_totals(
            month=self.month, participant_ids=[self.reader.pk],
            effective_from=effective_from, effective_to=effective_to,
        )[self.reader.pk]

    def test_confirmed_match_creates_exact_stable_submission_linked_reward_once(self):
        book = self.botm(); submission = self.submission()
        synchronize_submission(submission); synchronize_submission(submission)
        match = BotmMatch.objects.get(); provenance = self.provenance()
        self.assertEqual(provenance.source_reference, botm_book_reward_source_reference(match))
        self.assertEqual((provenance.month, provenance.participant, provenance.submission), (self.month, self.reader, submission))
        self.assertEqual((provenance.amount, provenance.effective_date, provenance.is_active), (book.bonus_pages, submission.completed_on, True))
        self.assertTrue(provenance.is_system_generated)
        self.assertEqual(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.BOTM_BOOK).count(), 1)

    def test_zero_bonus_creates_no_zero_or_active_reward(self):
        self.botm(bonus=0); submission = self.submission(); synchronize_submission(submission)
        self.assertTrue(BotmMatch.objects.filter(status=BotmMatch.Status.CONFIRMED, is_qualifying=True).exists())
        self.assertFalse(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.BOTM_BOOK).exists())

    def ambiguous(self):
        first_catalog = CatalogBook.objects.create(provider="hardcover", provider_book_id="reward-first", title="Reward Book", author="Reward Author")
        second_catalog = CatalogBook.objects.create(provider="hardcover", provider_book_id="reward-second", title="Reward Book", author="Reward Author")
        first_edition = CatalogEdition.objects.create(book=first_catalog, provider="hardcover", provider_edition_id="reward-first-ed", page_count=300)
        second_edition = CatalogEdition.objects.create(book=second_catalog, provider="hardcover", provider_edition_id="reward-second-ed", page_count=300)
        self.botm(position=1, catalog_book=first_catalog, catalog_edition=first_edition)
        self.botm(position=2, catalog_book=second_catalog, catalog_edition=second_edition)
        submission = self.submission(); synchronize_submission(submission)
        return submission

    def test_pending_and_rejected_matches_create_no_reward_manual_confirmation_does(self):
        submission = self.ambiguous()
        self.assertFalse(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.BOTM_BOOK).exists())
        first, second = BotmMatch.objects.order_by("pk")
        adjudicate_match(match=first, actor=self.host_user, decision=BotmMatch.Status.REJECTED)
        self.assertFalse(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.BOTM_BOOK).exists())
        adjudicate_match(match=second, actor=self.host_user, decision=BotmMatch.Status.CONFIRMED)
        provenance = self.provenance()
        self.assertEqual((provenance.submission, provenance.amount, provenance.is_active), (submission, 50, True))

    def test_rejection_removal_and_reapproval_reuse_one_durable_reward(self):
        self.botm(); submission = self.submission(); synchronize_submission(submission)
        match_pk = BotmMatch.objects.get().pk; provenance_pk = self.provenance().pk
        submission.status = BookSubmission.Status.REJECTED; submission.approved_pages = None; submission.save(); synchronize_submission(submission)
        self.assertFalse(ModifierProvenance.objects.get(pk=provenance_pk).is_active)
        submission.status = BookSubmission.Status.APPROVED; submission.approved_pages = 300; submission.save(); synchronize_submission(submission)
        self.assertTrue(ModifierProvenance.objects.get(pk=provenance_pk).is_active)
        submission.is_removed = True; submission.save(update_fields=["is_removed"]); synchronize_submission(submission)
        self.assertFalse(ModifierProvenance.objects.get(pk=provenance_pk).is_active)
        self.assertEqual(BotmMatch.objects.get().pk, match_pk)
        self.assertEqual(ModifierProvenance.objects.count(), 1)

    def test_submission_review_boundary_returns_current_botm_cache(self):
        self.botm(); submission = self.submission(status=BookSubmission.Status.PENDING)
        submission.status = BookSubmission.Status.APPROVED
        submission.approved_pages = 300
        reviewed = apply_submission_review(submission, [], self.host_user, timezone.now())
        self.assertEqual((reviewed.bonus_pages, reviewed.final_scored_pages), (50, 350))
        reviewed.status = BookSubmission.Status.REJECTED
        reviewed.approved_pages = None
        reviewed = apply_submission_review(reviewed, [], self.host_user, timezone.now())
        self.assertEqual((reviewed.bonus_pages, reviewed.final_scored_pages), (0, None))
        self.assertFalse(self.provenance().is_active)

    def test_theme_botm_and_standalone_game_stack_without_double_counting(self):
        self.botm(); submission = self.submission()
        theme = MonthTheme.objects.create(bonus_rule="legacy_fixed",
            month=self.month, name="Theme", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), bonus_pages=25,
        )
        ThemeClaim.objects.create(
            submission=submission, theme=theme, status=ThemeClaim.Status.APPROVED,
            approved_bonus_pages=25, reviewed_by=self.host_user, reviewed_at=timezone.now(),
        )
        synchronize_submission(submission)
        ModifierProvenance.objects.create(
            month=self.month, participant=self.reader, submission=None,
            source_type=ModifierProvenance.SourceType.GAME_REWARD,
            source_reference="game_reward_recipient:botm-stack", source_label="Game",
            amount=40, effective_date=date(2026, 8, 16), is_active=True,
        )
        refresh_submission_score(submission); submission.refresh_from_db()
        self.assertEqual((submission.approved_pages, submission.bonus_pages, submission.final_scored_pages), (300, 75, 375))
        self.assertEqual(self.score(), {"base_pages": 300, "modifier_pages": 115, "total_pages": 415})

    def test_inactivation_removes_exact_botm_amount_without_changing_base(self):
        self.botm(); submission = self.submission(); synchronize_submission(submission)
        self.assertEqual(self.score(), {"base_pages": 300, "modifier_pages": 50, "total_pages": 350})
        submission.is_removed = True; submission.save(update_fields=["is_removed"]); synchronize_submission(submission)
        self.assertEqual(self.score(), {"base_pages": 0, "modifier_pages": 0, "total_pages": 0})
        submission.is_removed = False; submission.save(update_fields=["is_removed"]); synchronize_submission(submission)
        self.assertEqual(self.score(), {"base_pages": 300, "modifier_pages": 50, "total_pages": 350})

    def test_date_range_uses_submission_completion_date(self):
        self.botm(); synchronize_submission(self.submission())
        self.assertEqual(self.score(effective_from=date(2026, 8, 10), effective_to=date(2026, 8, 20))["modifier_pages"], 50)
        self.assertEqual(self.score(effective_from=date(2026, 8, 16), effective_to=date(2026, 8, 31)), {"base_pages": 0, "modifier_pages": 0, "total_pages": 0})

    def test_retirement_follows_matching_model_and_preserves_inactive_history(self):
        book = self.botm(); submission = self.submission(); synchronize_submission(submission)
        match_pk = BotmMatch.objects.get().pk; provenance_pk = self.provenance().pk
        retire_botm_book(month=self.month, book=book, actor=self.host_user)
        self.assertFalse(BotmMatch.objects.get(pk=match_pk).is_qualifying)
        self.assertFalse(ModifierProvenance.objects.get(pk=provenance_pk).is_active)
        self.assertEqual((BotmMatch.objects.count(), ModifierProvenance.objects.count()), (1, 1))

    def test_batch_reward_apis_are_idempotent(self):
        self.botm(); submission = self.submission(); synchronize_submission(submission)
        match = BotmMatch.objects.get()
        synchronize_botm_book_reward(match)
        synchronize_botm_book_rewards_for_submission(submission)
        synchronize_botm_book_rewards_for_reader(month=self.month, participant=self.reader)
        synchronize_botm_book_rewards_for_challenge(self.month)
        self.assertEqual(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.BOTM_BOOK).count(), 1)

    def test_corrupt_reward_ownership_is_rejected_not_rewritten(self):
        self.botm(); submission = self.submission(); synchronize_submission(submission)
        provenance = self.provenance()
        other_user = get_user_model().objects.create_user("other-reward-reader")
        other = Membership.objects.create(group=self.group, user=other_user, display_name="Other")
        ModifierProvenance.objects.filter(pk=provenance.pk).update(participant=other)
        with self.assertRaises(ValidationError):
            synchronize_botm_book_reward(BotmMatch.objects.get())

    def test_no_completion_or_tbr_provenance_is_created(self):
        self.botm(); synchronize_submission(self.submission())
        forbidden = [
            ModifierProvenance.SourceType.BOTM_COMPLETION,
            ModifierProvenance.SourceType.TBR_BOOK,
            ModifierProvenance.SourceType.TBR_COMPLETION,
        ]
        self.assertFalse(ModifierProvenance.objects.filter(source_type__in=forbidden).exists())
