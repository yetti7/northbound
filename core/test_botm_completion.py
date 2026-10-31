from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .botm_completion import (
    botm_completion_source_reference,
    completion_set_fingerprint,
    synchronize_botm_completion_for_reader,
    synchronize_botm_completions_for_challenge,
)
from .botm_configuration import retire_botm_book
from .botm_matching import synchronize_submission
from .models import (
    BookSubmission, BotmBook, BotmCompletionAward, BotmMatch, ChallengeMonth,
    ChallengeStaffAssignment, Membership, ModifierProvenance, MonthEnrollment,
    MonthTheme, ProgressCheckpoint, ReadingGroup, ThemeClaim,
)
from .participation import activate_participation, deactivate_participation
from .progress_checkpoints import process_due_progress_checkpoints
from .score_aggregation import challenge_score_totals


class BotmCompletionRewardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.host_user = User.objects.create_user("completion-host")
        self.reader_user = User.objects.create_user("completion-reader")
        self.floater_user = User.objects.create_user("completion-floater")
        self.root = User.objects.create_superuser("completion-root", "root@example.com", "password")
        self.group = ReadingGroup.objects.create(name="BOTM Completion", slug="botm-completion")
        self.host = Membership.objects.create(group=self.group, user=self.host_user, display_name="Host")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Reader")
        self.floater = Membership.objects.create(group=self.group, user=self.floater_user, display_name="Floater")
        self.root_membership = Membership.objects.create(group=self.group, user=self.root, display_name="Root")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="Completion Challenge", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.ACTIVE,
            botm_enabled=True, botm_completion_bonus_pages=200,
        )
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER)
        self.enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader)

    def books(self, count, *, per_book_bonus=10):
        existing = list(self.month.botm_books.filter(is_retired=False).order_by("position"))
        for position in range(len(existing) + 1, count + 1):
            existing.append(BotmBook.objects.create(
                month=self.month, position=position, title_snapshot=f"BOTM {position}",
                author_snapshot=f"Author {position}", page_count_snapshot=100,
                bonus_pages=per_book_bonus,
            ))
        return existing

    def submit_for(self, book):
        submission = BookSubmission.objects.create(
            month=self.month, participant=self.reader, title=book.title_snapshot,
            author=book.author_snapshot, book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 8, min(28, book.position + 1)), submitted_pages=100,
            approved_pages=100, status=BookSubmission.Status.APPROVED,
        )
        synchronize_submission(submission)
        return submission

    def qualify(self, count, *, per_book_bonus=10):
        books = self.books(count, per_book_bonus=per_book_bonus)
        submissions = [self.submit_for(book) for book in books]
        return books, submissions

    def completion_provenance(self):
        return ModifierProvenance.objects.get(source_type=ModifierProvenance.SourceType.BOTM_COMPLETION)

    def score(self):
        return challenge_score_totals(month=self.month, participant_ids=[self.reader.pk])[self.reader.pk]

    def test_denominator_is_configured_set_for_one_three_and_nine(self):
        books, _ = self.qualify(1)
        award = BotmCompletionAward.objects.get()
        self.assertEqual((award.configured_book_count, award.configured_books.count()), (1, 1))

        other_user = get_user_model().objects.create_user("three-reader")
        other = Membership.objects.create(group=self.group, user=other_user, display_name="Three")
        MonthEnrollment.objects.create(month=self.month, participant=other)
        for book in self.books(3):
            submission = BookSubmission.objects.create(
                month=self.month, participant=other, title=book.title_snapshot, author=book.author_snapshot,
                book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 10),
                submitted_pages=100, approved_pages=100, status=BookSubmission.Status.APPROVED,
            )
            synchronize_submission(submission)
        three = BotmCompletionAward.objects.get(participant=other)
        self.assertEqual(three.configured_book_count, 3)

        nine_user = get_user_model().objects.create_user("nine-reader")
        nine = Membership.objects.create(group=self.group, user=nine_user, display_name="Nine")
        MonthEnrollment.objects.create(month=self.month, participant=nine)
        all_books = self.books(9)
        for book in all_books[:8]:
            submission = BookSubmission.objects.create(
                month=self.month, participant=nine, title=book.title_snapshot, author=book.author_snapshot,
                book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 12),
                submitted_pages=100, approved_pages=100, status=BookSubmission.Status.APPROVED,
            )
            synchronize_submission(submission)
        self.assertFalse(BotmCompletionAward.objects.filter(participant=nine).exists())
        final = BookSubmission.objects.create(
            month=self.month, participant=nine, title=all_books[8].title_snapshot, author=all_books[8].author_snapshot,
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 13),
            submitted_pages=100, approved_pages=100, status=BookSubmission.Status.APPROVED,
        )
        synchronize_submission(final)
        self.assertEqual(BotmCompletionAward.objects.get(participant=nine).configured_book_count, 9)

    def test_missing_one_does_not_complete(self):
        books = self.books(3)
        self.submit_for(books[0]); self.submit_for(books[1])
        self.assertFalse(BotmCompletionAward.objects.exists())

    def test_award_freezes_exact_set_amount_time_and_standalone_provenance(self):
        before = timezone.now(); books, _ = self.qualify(3); after = timezone.now()
        award = BotmCompletionAward.objects.get(); provenance = self.completion_provenance()
        self.assertEqual(award.completion_set_fingerprint, completion_set_fingerprint(books))
        self.assertEqual(set(award.configured_books.values_list("botm_book_id", flat=True)), {book.pk for book in books})
        self.assertEqual((award.configured_book_count, award.bonus_amount_snapshot), (3, 200))
        self.assertLessEqual(before, award.qualified_at); self.assertLessEqual(award.qualified_at, after)
        self.assertEqual(provenance.source_reference, botm_completion_source_reference(award))
        self.assertEqual((provenance.month, provenance.participant, provenance.submission), (self.month, self.reader, None))
        self.assertEqual((provenance.amount, provenance.effective_date, provenance.is_active), (200, award.effective_date, True))
        synchronize_botm_completion_for_reader(month=self.month, participant=self.reader)
        self.assertEqual((BotmCompletionAward.objects.count(), ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.BOTM_COMPLETION).count()), (1, 1))

    def test_zero_bonus_preserves_logical_completion_without_provenance(self):
        self.month.botm_completion_bonus_pages = 0; self.month.save(update_fields=["botm_completion_bonus_pages"])
        self.qualify(1)
        award = BotmCompletionAward.objects.get()
        self.assertTrue(award.is_qualifying); self.assertEqual(award.bonus_amount_snapshot, 0)
        self.assertFalse(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.BOTM_COMPLETION).exists())

    def test_rejection_removal_and_reapproval_reuse_history_and_provenance(self):
        _, submissions = self.qualify(2)
        award_pk = BotmCompletionAward.objects.get().pk; provenance_pk = self.completion_provenance().pk
        submission = submissions[0]
        submission.status = BookSubmission.Status.REJECTED; submission.approved_pages = None; submission.save(); synchronize_submission(submission)
        self.assertFalse(BotmCompletionAward.objects.get(pk=award_pk).is_qualifying)
        self.assertFalse(ModifierProvenance.objects.get(pk=provenance_pk).is_active)
        submission.status = BookSubmission.Status.APPROVED; submission.approved_pages = 100; submission.save(); synchronize_submission(submission)
        self.assertTrue(BotmCompletionAward.objects.get(pk=award_pk).is_qualifying)
        self.assertTrue(ModifierProvenance.objects.get(pk=provenance_pk).is_active)
        submissions[1].is_removed = True; submissions[1].save(update_fields=["is_removed"]); synchronize_submission(submissions[1])
        self.assertFalse(BotmCompletionAward.objects.get(pk=award_pk).is_qualifying)
        submissions[1].is_removed = False; submissions[1].save(update_fields=["is_removed"]); synchronize_submission(submissions[1])
        self.assertEqual((BotmCompletionAward.objects.count(), ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.BOTM_COMPLETION).count()), (1, 1))

    def test_withdraw_disable_and_reenable_preserve_and_reactivate_same_award(self):
        self.qualify(1); award_pk = BotmCompletionAward.objects.get().pk; provenance_pk = self.completion_provenance().pk
        deactivate_participation(enrollment=self.enrollment, actor=self.reader_user, reason=MonthEnrollment.InactiveReason.WITHDRAWN)
        self.assertFalse(BotmCompletionAward.objects.get(pk=award_pk).is_qualifying)
        activate_participation(month=self.month, participant=self.reader, actor=self.reader_user, origin=MonthEnrollment.Origin.SELF)
        self.assertTrue(BotmCompletionAward.objects.get(pk=award_pk).is_qualifying)
        self.month.botm_enabled = False; self.month.save(update_fields=["botm_enabled"])
        synchronize_botm_completions_for_challenge(self.month)
        self.assertFalse(ModifierProvenance.objects.get(pk=provenance_pk).is_active)
        self.month.botm_enabled = True; self.month.save(update_fields=["botm_enabled"])
        synchronize_botm_completions_for_challenge(self.month)
        self.assertTrue(ModifierProvenance.objects.get(pk=provenance_pk).is_active)
        self.assertEqual(BotmCompletionAward.objects.count(), 1)

    def test_configuration_sets_are_distinct_and_historical_amounts_are_frozen(self):
        first_books, _ = self.qualify(2)
        first = BotmCompletionAward.objects.get(); first_provenance = self.completion_provenance()
        self.month.botm_completion_bonus_pages = 300; self.month.save(update_fields=["botm_completion_bonus_pages"])
        retire_botm_book(month=self.month, book=first_books[1], actor=self.host_user)
        one_book = BotmCompletionAward.objects.exclude(pk=first.pk).get()
        self.assertEqual((first.configured_book_count, first.bonus_amount_snapshot, first_provenance.amount), (2, 200, 200))
        self.assertEqual((one_book.configured_book_count, one_book.bonus_amount_snapshot), (1, 300))
        replacement = BotmBook.objects.create(
            month=self.month, position=2, title_snapshot="Replacement", author_snapshot="New Author",
            page_count_snapshot=100, bonus_pages=10,
        )
        synchronize_botm_completions_for_challenge(self.month)
        self.assertFalse(one_book.__class__.objects.get(pk=one_book.pk).is_qualifying)
        self.submit_for(replacement)
        current = BotmCompletionAward.objects.filter(is_qualifying=True).get()
        self.assertEqual((current.configured_book_count, current.bonus_amount_snapshot), (2, 300))
        self.assertNotEqual(first.completion_set_fingerprint, current.completion_set_fingerprint)
        self.assertEqual(BotmCompletionAward.objects.count(), 3)

    def test_completion_stacks_but_never_enters_submission_cache(self):
        books = self.books(2, per_book_bonus=50)
        first = self.submit_for(books[0])
        theme = MonthTheme.objects.create(bonus_rule="legacy_fixed", month=self.month, name="Theme", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), bonus_pages=25)
        ThemeClaim.objects.create(submission=first, theme=theme, status=ThemeClaim.Status.APPROVED, approved_bonus_pages=25)
        first.recalculate_score()
        second = self.submit_for(books[1])
        ModifierProvenance.objects.create(
            month=self.month, participant=self.reader, submission=None,
            source_type=ModifierProvenance.SourceType.GAME_REWARD,
            source_reference="game_reward_recipient:completion-stack", source_label="Game",
            amount=75, effective_date=date(2026, 8, 20),
        )
        first.refresh_from_db(); second.refresh_from_db()
        self.assertEqual((first.bonus_pages, first.final_scored_pages), (75, 175))
        self.assertEqual((second.bonus_pages, second.final_scored_pages), (50, 150))
        self.assertEqual(self.score(), {"base_pages": 200, "modifier_pages": 400, "total_pages": 600})

    def test_effective_date_filtering_and_checkpoint_snapshot_behavior(self):
        book = self.books(1, per_book_bonus=0)[0]
        submission = BookSubmission.objects.create(
            month=self.month, participant=self.reader, title=book.title_snapshot, author=book.author_snapshot,
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 10), submitted_pages=100,
            approved_pages=100, status=BookSubmission.Status.APPROVED,
        )
        BotmMatch.objects.create(
            botm_book=book, month=self.month, participant=self.reader, submission=submission,
            method=BotmMatch.Method.NORMALIZED_TITLE_AUTHOR, status=BotmMatch.Status.CONFIRMED,
            is_qualifying=True, botm_title_snapshot=book.title_snapshot, botm_author_snapshot=book.author_snapshot,
            submission_title_snapshot=submission.title, submission_author_snapshot=submission.author,
        )
        completed_at = datetime(2026, 8, 20, 15, tzinfo=ZoneInfo("America/New_York"))
        award = synchronize_botm_completion_for_reader(month=self.month, participant=self.reader, now=completed_at)
        before = challenge_score_totals(month=self.month, participant_ids=[self.reader.pk], effective_to=date(2026, 8, 19))[self.reader.pk]
        including = challenge_score_totals(month=self.month, participant_ids=[self.reader.pk], effective_to=date(2026, 8, 20))[self.reader.pk]
        self.assertEqual((before["modifier_pages"], including["modifier_pages"], award.effective_date), (0, 200, date(2026, 8, 20)))
        checkpoint = ProgressCheckpoint.objects.create(
            month=self.month, scheduled_at=completed_at, threshold_percentage=50,
            progress_basis=ProgressCheckpoint.ProgressBasis.TOTAL,
            target_basis=ProgressCheckpoint.TargetBasis.FIXED, fixed_target_pages=400,
        )
        process_due_progress_checkpoints(now=completed_at + timedelta(minutes=1))
        result = checkpoint.results.get(participant=self.reader)
        self.assertEqual(result.progress_pages, 300)
        base_checkpoint = ProgressCheckpoint.objects.create(
            month=self.month, scheduled_at=completed_at, threshold_percentage=50,
            progress_basis=ProgressCheckpoint.ProgressBasis.BASE,
            target_basis=ProgressCheckpoint.TargetBasis.FIXED, fixed_target_pages=400, position=2,
        )
        process_due_progress_checkpoints(now=completed_at + timedelta(minutes=2))
        self.assertEqual(base_checkpoint.results.get(participant=self.reader).progress_pages, 100)
        provenance = self.completion_provenance()
        provenance.is_active = False
        provenance.save(update_fields=["is_active"])
        result.refresh_from_db(); self.assertEqual(result.progress_pages, 300)

    def test_nonreaders_do_not_earn_automatic_completion(self):
        book = self.books(1)[0]
        for participant in (self.host, self.floater, self.root_membership):
            synchronize_botm_completion_for_reader(month=self.month, participant=participant)
        self.assertFalse(BotmCompletionAward.objects.exists())
        MonthEnrollment.objects.create(month=self.month, participant=self.host)
        host_submission = BookSubmission.objects.create(
            month=self.month, participant=self.host, title=book.title_snapshot, author=book.author_snapshot,
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 20),
            submitted_pages=100, approved_pages=100, status=BookSubmission.Status.APPROVED,
        )
        synchronize_submission(host_submission)
        self.assertTrue(BotmCompletionAward.objects.filter(participant=self.host, is_qualifying=True).exists())

    def test_operator_history_is_private_and_read_only(self):
        self.qualify(1)
        url = reverse("botm-match-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.client.force_login(self.host_user)
        response = self.client.get(url)
        self.assertContains(response, "Completion Awards"); self.assertContains(response, "200 pages")
        self.assertNotContains(response, "Mark Complete")
        self.client.force_login(self.reader_user); self.assertEqual(self.client.get(url).status_code, 403)

    def test_no_tbr_or_final_posters_interface_is_introduced(self):
        self.qualify(1)
        self.assertFalse(ModifierProvenance.objects.filter(source_type__in=[ModifierProvenance.SourceType.TBR_BOOK, ModifierProvenance.SourceType.TBR_COMPLETION]).exists())
        self.client.force_login(self.host_user)
        response = self.client.get(reverse("botm-match-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertNotContains(response, "Posters")
