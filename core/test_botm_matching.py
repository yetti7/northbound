from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .botm_configuration import delete_unused_botm_book, retire_botm_book, update_botm_book
from .botm_matching import adjudicate_match, synchronize_challenge, synchronize_submission
from .models import (
    AuditEvent, BookSubmission, BotmBook, BotmMatch, CatalogBook, CatalogEdition,
    ChallengeMonth, ChallengeStaffAssignment, Membership, ModifierProvenance,
    MonthEnrollment, ReadingGroup, Team, TeamAssignment,
)


class BotmMatchingFoundationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.root = User.objects.create_superuser("match-root", "root@example.com", "password")
        self.host_user = User.objects.create_user("match-host")
        self.reader_user = User.objects.create_user("match-reader")
        self.owner_user = User.objects.create_user("match-owner")
        self.moderator_user = User.objects.create_user("match-moderator")
        self.floater_user = User.objects.create_user("match-floater")
        self.leader_user = User.objects.create_user("match-leader")
        self.ended_user = User.objects.create_user("match-ended")
        self.group = ReadingGroup.objects.create(name="BOTM Matching", slug="botm-matching")
        self.host = self.member(self.host_user, "Host")
        self.reader = self.member(self.reader_user, "Reader")
        self.owner = self.member(self.owner_user, "Owner", Membership.Role.OWNER)
        self.moderator = self.member(self.moderator_user, "Moderator", Membership.Role.MODERATOR)
        self.floater = self.member(self.floater_user, "Floater")
        self.leader = self.member(self.leader_user, "Leader")
        self.ended = self.member(self.ended_user, "Ended")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="Match Challenge", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.ACTIVE, botm_enabled=True,
        )
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.ended, role=ChallengeStaffAssignment.Role.HOST, ended_at=timezone.now())
        MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        MonthEnrollment.objects.create(month=self.month, participant=self.leader)
        team = Team.objects.create(month=self.month, name="Readers")
        TeamAssignment.objects.create(month=self.month, team=team, participant=self.leader)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.leader, team=team, role=ChallengeStaffAssignment.Role.TEAM_LEADER)

    def member(self, user, name, role=Membership.Role.MEMBER):
        return Membership.objects.create(group=self.group, user=user, display_name=name, role=role)

    def catalog(self, key, title="Shared Work", author="A. Writer"):
        book = CatalogBook.objects.create(provider="hardcover", provider_book_id=key, title=title, author=author)
        edition = CatalogEdition.objects.create(book=book, provider="hardcover", provider_edition_id=f"{key}-edition", page_count=300)
        return book, edition

    def botm(self, *, position=1, title="Shared Work", author="A. Writer", catalog_book=None, catalog_edition=None):
        return BotmBook.objects.create(
            month=self.month, position=position, catalog_book=catalog_book,
            catalog_edition=catalog_edition, title_snapshot=title, author_snapshot=author,
            page_count_snapshot=300, bonus_pages=25,
        )

    def submission(self, *, title="Shared Work", author="A. Writer", catalog_book=None, catalog_edition=None, status=BookSubmission.Status.APPROVED, participant=None, month=None, removed=False):
        return BookSubmission.objects.create(
            month=month or self.month, participant=participant or self.reader,
            catalog_book=catalog_book, catalog_edition=catalog_edition,
            title=title, author=author, book_format=BookSubmission.Format.PAPERBACK,
            completed_on=date(2026, 8, 15), submitted_pages=300,
            approved_pages=300 if status == BookSubmission.Status.APPROVED else None,
            status=status, is_removed=removed,
        )

    def test_canonical_work_confirms_across_different_editions_and_is_idempotent(self):
        catalog, first_edition = self.catalog("work")
        second_edition = CatalogEdition.objects.create(book=catalog, provider="hardcover", provider_edition_id="other-edition", page_count=320)
        book = self.botm(catalog_book=catalog, catalog_edition=first_edition)
        submission = self.submission(catalog_book=catalog, catalog_edition=second_edition)
        synchronize_submission(submission); synchronize_submission(submission)
        match = BotmMatch.objects.get()
        self.assertEqual((match.botm_book, match.method, match.status, match.is_qualifying), (book, BotmMatch.Method.CATALOG_WORK, BotmMatch.Status.CONFIRMED, True))
        self.assertEqual(BotmMatch.objects.count(), 1)

    def test_different_catalog_work_does_not_fallback_or_match(self):
        botm_catalog, edition = self.catalog("botm")
        submission_catalog, submission_edition = self.catalog("submission")
        self.botm(catalog_book=botm_catalog, catalog_edition=edition)
        synchronize_submission(self.submission(catalog_book=submission_catalog, catalog_edition=submission_edition))
        self.assertFalse(BotmMatch.objects.exists())

    def test_unique_normalized_title_author_variants_confirm(self):
        self.botm(title="The Shared—Work!", author="A.  WRITER")
        submission = self.submission(title=" the shared work ", author="a writer")
        synchronize_submission(submission)
        self.assertEqual(BotmMatch.objects.get().method, BotmMatch.Method.NORMALIZED_TITLE_AUTHOR)

    def test_fallback_never_uses_title_alone_author_guess_subtitle_or_initial_expansion(self):
        self.botm(title="Shared Work: A Novel", author="Alice Writer")
        for title, author in (("Shared Work", "Alice Writer"), ("Shared Work: A Novel", "A Writer"), ("Shared Work: A Novel", "Other Author")):
            submission = self.submission(title=title, author=author)
            synchronize_submission(submission)
        self.assertFalse(BotmMatch.objects.exists())

    def test_multiple_normalized_candidates_are_pending_not_confirmed(self):
        first_catalog, first_edition = self.catalog("first")
        second_catalog, second_edition = self.catalog("second")
        self.botm(position=1, catalog_book=first_catalog, catalog_edition=first_edition)
        self.botm(position=2, catalog_book=second_catalog, catalog_edition=second_edition)
        synchronize_submission(self.submission())
        self.assertEqual(BotmMatch.objects.filter(status=BotmMatch.Status.PENDING_REVIEW).count(), 2)
        self.assertFalse(BotmMatch.objects.filter(is_qualifying=True).exists())

    def test_submission_status_removed_and_enrollment_eligibility(self):
        self.botm()
        pending = self.submission(status=BookSubmission.Status.PENDING)
        rejected = self.submission(status=BookSubmission.Status.REJECTED)
        removed = self.submission(removed=True)
        inactive_user = get_user_model().objects.create_user("inactive-reader")
        inactive = self.member(inactive_user, "Inactive")
        ineligible = self.submission(participant=inactive)
        for submission in (pending, rejected, removed, ineligible):
            synchronize_submission(submission)
        self.assertFalse(BotmMatch.objects.exists())

    def test_cross_challenge_and_reader_relationship_cannot_be_fabricated(self):
        other = ChallengeMonth.objects.create(group=self.group, name="Other", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.ACTIVE, botm_enabled=True)
        MonthEnrollment.objects.create(month=other, participant=self.reader)
        self.botm()
        synchronize_submission(self.submission(month=other))
        self.assertFalse(BotmMatch.objects.exists())
        match = BotmMatch(botm_book=self.month.botm_books.get(), month=self.month, participant=self.host, submission=self.submission(), method=BotmMatch.Method.CATALOG_WORK, status=BotmMatch.Status.CONFIRMED, is_qualifying=True)
        with self.assertRaises(ValidationError):
            match.full_clean()

    def test_rejection_removal_and_reapproval_preserve_and_reactivate_history(self):
        self.botm(); submission = self.submission(); synchronize_submission(submission)
        match_id = BotmMatch.objects.get().pk
        submission.status = BookSubmission.Status.REJECTED; submission.approved_pages = None; submission.save(); synchronize_submission(submission)
        match = BotmMatch.objects.get(); self.assertFalse(match.is_qualifying)
        submission.status = BookSubmission.Status.APPROVED; submission.approved_pages = 300; submission.save(); synchronize_submission(submission)
        match.refresh_from_db(); self.assertEqual(match.pk, match_id); self.assertTrue(match.is_qualifying)
        submission.is_removed = True; submission.save(update_fields=["is_removed"]); synchronize_submission(submission)
        match.refresh_from_db(); self.assertFalse(match.is_qualifying); self.assertEqual(match.status, BotmMatch.Status.CONFIRMED)

    def test_retired_book_gets_no_new_match_and_existing_history_survives(self):
        book = self.botm(); submission = self.submission(); synchronize_submission(submission)
        retire_botm_book(month=self.month, book=book, actor=self.host_user)
        self.assertEqual(BotmMatch.objects.count(), 1)
        self.assertFalse(BotmMatch.objects.get().is_qualifying)
        synchronize_submission(self.submission())
        self.assertEqual(BotmMatch.objects.count(), 1)

    def test_used_book_identity_bonus_and_delete_are_locked_but_position_is_editable(self):
        book = self.botm(); synchronize_submission(self.submission())
        values = {"position": 2, "title_snapshot": book.title_snapshot, "author_snapshot": book.author_snapshot, "page_count_snapshot": 300, "cover_url_snapshot": "", "source_url_snapshot": "", "bonus_pages": 25}
        update_botm_book(month=self.month, book=book, actor=self.host_user, values=values)
        book.refresh_from_db(); self.assertEqual(book.position, 2)
        for field, value in (("title_snapshot", "Changed"), ("author_snapshot", "Changed"), ("page_count_snapshot", 301), ("bonus_pages", 30)):
            changed = dict(values); changed[field] = value
            with self.assertRaises(ValidationError):
                update_botm_book(month=self.month, book=book, actor=self.host_user, values=changed)
        with self.assertRaises(ValidationError):
            delete_unused_botm_book(month=self.month, book=book, actor=self.host_user)

    def test_unused_book_remains_editable_and_deletable(self):
        book = self.botm()
        update_botm_book(month=self.month, book=book, actor=self.host_user, values={"position": 1, "title_snapshot": "Changed", "author_snapshot": "New Author", "page_count_snapshot": 301, "cover_url_snapshot": "", "source_url_snapshot": "", "bonus_pages": 30})
        delete_unused_botm_book(month=self.month, book=book, actor=self.host_user)
        self.assertFalse(BotmBook.objects.exists())

    def pending(self):
        first_catalog, first_edition = self.catalog("first")
        second_catalog, second_edition = self.catalog("second")
        self.botm(position=1, catalog_book=first_catalog, catalog_edition=first_edition)
        self.botm(position=2, catalog_book=second_catalog, catalog_edition=second_edition)
        submission = self.submission(); synchronize_submission(submission)
        return BotmMatch.objects.order_by("pk").first(), submission

    def test_host_and_platform_owner_can_confirm_or_reject_with_audit(self):
        match, _ = self.pending()
        adjudicate_match(match=match, actor=self.host_user, decision=BotmMatch.Status.CONFIRMED)
        match.refresh_from_db(); self.assertTrue(match.is_qualifying); self.assertEqual(match.method, BotmMatch.Method.MANUAL_REVIEW)
        synchronize_submission(match.submission)
        match.refresh_from_db(); self.assertTrue(match.is_qualifying)
        other = BotmMatch.objects.exclude(pk=match.pk).get()
        adjudicate_match(match=other, actor=self.root, decision=BotmMatch.Status.REJECTED)
        self.assertTrue(AuditEvent.objects.filter(action="botm.match_confirmed").exists())
        self.assertTrue(AuditEvent.objects.filter(action="botm.match_rejected").exists())

    def test_unauthorized_roles_and_ended_host_cannot_adjudicate(self):
        match, _ = self.pending()
        for user in (self.owner_user, self.moderator_user, self.floater_user, self.leader_user, self.reader_user, self.ended_user):
            with self.assertRaises(ValidationError):
                adjudicate_match(match=match, actor=user, decision=BotmMatch.Status.CONFIRMED)

    def test_rejected_candidate_is_durable_and_not_recreated(self):
        match, submission = self.pending()
        adjudicate_match(match=match, actor=self.host_user, decision=BotmMatch.Status.REJECTED)
        count = BotmMatch.objects.count(); synchronize_submission(submission); synchronize_submission(submission)
        match.refresh_from_db(); self.assertEqual(match.status, BotmMatch.Status.REJECTED); self.assertEqual(BotmMatch.objects.count(), count)

    def test_duplicate_constraints_prevent_two_active_completions(self):
        first = self.botm(position=1); submission = self.submission(); synchronize_submission(submission)
        second = self.botm(position=2, title="Other", author="Other")
        values = {
            "botm_book": second, "month": self.month, "participant": self.reader, "submission": submission,
            "method": BotmMatch.Method.MANUAL_REVIEW, "status": BotmMatch.Status.CONFIRMED,
            "is_qualifying": True, "botm_title_snapshot": "Other", "botm_author_snapshot": "Other",
            "submission_title_snapshot": submission.title, "submission_author_snapshot": submission.author,
        }
        with self.assertRaises((ValidationError, IntegrityError)):
            with transaction.atomic():
                BotmMatch.objects.create(**values)
        other_submission = self.submission(title="Other", author="Other")
        values.update(submission=other_submission, botm_book=first)
        with self.assertRaises((ValidationError, IntegrityError)):
            with transaction.atomic():
                BotmMatch.objects.create(**values)

    def test_match_reward_changes_only_modifier_cache_and_creates_no_completion_provenance(self):
        self.botm(); submission = self.submission()
        synchronize_submission(submission); submission.refresh_from_db()
        self.assertEqual((submission.approved_pages, submission.bonus_pages, submission.final_scored_pages), (300, 25, 325))
        self.assertEqual(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.BOTM_BOOK).count(), 1)
        self.assertFalse(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.BOTM_COMPLETION).exists())

    def test_operator_ui_is_private_and_contains_no_reader_hidden_evidence(self):
        self.botm(); synchronize_submission(self.submission())
        url = reverse("botm-match-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        for user in (self.owner_user, self.moderator_user, self.floater_user, self.leader_user, self.reader_user, self.ended_user):
            self.client.force_login(user); self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.host_user); self.assertContains(self.client.get(url), "Match History")
        self.client.force_login(self.root); self.assertContains(self.client.get(url), "Shared Work")
        self.client.force_login(self.reader_user)
        self.assertNotContains(self.client.get(reverse("botm-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})), "Match History")

    def test_host_review_routes_confirm_and_reject_pending_candidates(self):
        match, _ = self.pending()
        self.client.force_login(self.host_user)
        confirm_url = reverse("botm-match-review", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": match.pk, "decision": "confirm"})
        self.assertEqual(self.client.get(confirm_url).status_code, 403)
        self.client.post(confirm_url)
        match.refresh_from_db(); self.assertEqual(match.status, BotmMatch.Status.CONFIRMED)
        other = BotmMatch.objects.exclude(pk=match.pk).get()
        reject_url = reverse("botm-match-review", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": other.pk, "decision": "reject"})
        self.client.post(reject_url)
        other.refresh_from_db(); self.assertEqual(other.status, BotmMatch.Status.REJECTED)

    def test_challenge_synchronization_processes_all_and_remains_idempotent(self):
        self.botm(); self.submission(); self.submission(title="No Match", author="Nobody")
        synchronize_challenge(self.month); synchronize_challenge(self.month)
        self.assertEqual(BotmMatch.objects.count(), 1)
