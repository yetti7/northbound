from datetime import date
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    AuditEvent, BookSubmission, CatalogBook, CatalogEdition, ChallengeMonth,
    ChallengeStaffAssignment, Membership, ModifierProvenance, MonthEnrollment,
    PersonalTBR, PersonalTBRBook, PersonalTBRMatch, ReadingGroup, Team, TeamAssignment,
)
from .personal_tbr_matching import adjudicate_match, select_candidates, synchronize_reader, synchronize_submission


class PersonalTbrMatchingFoundationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.root = User.objects.create_superuser("tbr-match-root", "root@example.com", "password")
        self.host_user = User.objects.create_user("tbr-match-host")
        self.floater_user = User.objects.create_user("tbr-match-floater")
        self.leader_user = User.objects.create_user("tbr-match-leader")
        self.reader_user = User.objects.create_user("tbr-match-reader")
        self.other_user = User.objects.create_user("tbr-match-other")
        self.owner_user = User.objects.create_user("tbr-match-owner")
        self.group = ReadingGroup.objects.create(name="TBR Matching", slug="tbr-matching")
        self.host = self.member(self.host_user, "Host")
        self.floater = self.member(self.floater_user, "Floater")
        self.leader = self.member(self.leader_user, "Leader")
        self.reader = self.member(self.reader_user, "Reader")
        self.other = self.member(self.other_user, "Other")
        self.owner = self.member(self.owner_user, "Owner", Membership.Role.OWNER)
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="TBR Match Challenge", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.ACTIVE, tbr_enabled=True,
        )
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER)
        self.enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        self.other_enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.other)
        MonthEnrollment.objects.create(month=self.month, participant=self.leader)
        self.team = Team.objects.create(month=self.month, name="Reader Team")
        self.other_team = Team.objects.create(month=self.month, name="Other Team")
        TeamAssignment.objects.create(month=self.month, team=self.team, participant=self.reader)
        TeamAssignment.objects.create(month=self.month, team=self.team, participant=self.leader)
        TeamAssignment.objects.create(month=self.month, team=self.other_team, participant=self.other)
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.leader, team=self.team, role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        )

    def member(self, user, display_name, role=Membership.Role.MEMBER):
        return Membership.objects.create(group=self.group, user=user, display_name=display_name, role=role)

    def catalog(self, key, title="Shared Work", author="A. Writer"):
        book = CatalogBook.objects.create(provider="hardcover", provider_book_id=key, title=title, author=author)
        edition = CatalogEdition.objects.create(
            book=book, provider="hardcover", provider_edition_id=f"{key}-edition", page_count=300,
        )
        return book, edition

    def locked_tbr(self, books, enrollment=None):
        tbr = PersonalTBR.objects.create(enrollment=enrollment or self.enrollment)
        result = []
        for position, values in enumerate(books, 1):
            result.append(PersonalTBRBook.objects.create(personal_tbr=tbr, position=position, **values))
        tbr.confirmed_at = timezone.now()
        tbr.save(update_fields=["confirmed_at"])
        return tbr, result

    def submission(self, *, participant=None, title="Shared Work", author="A. Writer", catalog_book=None,
                   catalog_edition=None, status=BookSubmission.Status.APPROVED, removed=False):
        return BookSubmission.objects.create(
            month=self.month, participant=participant or self.reader, title=title, author=author,
            catalog_book=catalog_book, catalog_edition=catalog_edition,
            book_format=BookSubmission.Format.PAPERBACK, completed_on=date(2026, 8, 15),
            submitted_pages=300, approved_pages=300 if status == BookSubmission.Status.APPROVED else None,
            status=status, is_removed=removed,
        )

    def test_canonical_work_matches_same_or_different_edition_without_rewriting_tbr(self):
        catalog, selected = self.catalog("work")
        other_edition = CatalogEdition.objects.create(
            book=catalog, provider="hardcover", provider_edition_id="other-edition", page_count=320,
        )
        _, books = self.locked_tbr([{
            "catalog_book": catalog, "catalog_edition": selected, "title_snapshot": "Shared Work",
            "author_snapshot": "A. Writer", "page_count_snapshot": 300,
        }])
        first = self.submission(catalog_book=catalog, catalog_edition=selected)
        synchronize_submission(first)
        match = PersonalTBRMatch.objects.get(submission=first)
        self.assertEqual((match.method, match.status, match.is_qualifying), (PersonalTBRMatch.Method.CATALOG_WORK, PersonalTBRMatch.Status.CONFIRMED, True))
        first.is_removed = True; first.save(update_fields=["is_removed"]); synchronize_submission(first)
        second = self.submission(catalog_book=catalog, catalog_edition=other_edition)
        synchronize_submission(second)
        books[0].refresh_from_db()
        self.assertEqual(books[0].catalog_edition, selected)
        self.assertTrue(PersonalTBRMatch.objects.get(submission=second).is_qualifying)

    def test_different_canonical_work_does_not_fallback_between_catalog_books(self):
        first, first_edition = self.catalog("first")
        second, second_edition = self.catalog("second")
        self.locked_tbr([{"catalog_book": first, "catalog_edition": first_edition, "title_snapshot": "Shared Work", "author_snapshot": "A. Writer"}])
        synchronize_submission(self.submission(catalog_book=second, catalog_edition=second_edition))
        self.assertFalse(PersonalTBRMatch.objects.exists())

    def test_strict_normalized_fallback_matches_manual_but_never_title_author_or_fuzzy_alone(self):
        self.locked_tbr([{"title_snapshot": "The Shared—Work!", "author_snapshot": "A.  WRITER"}])
        matching = self.submission(title=" the shared work ", author="a writer")
        synchronize_submission(matching)
        self.assertEqual(PersonalTBRMatch.objects.get().method, PersonalTBRMatch.Method.NORMALIZED_TITLE_AUTHOR)
        for title, author in (("The Shared Work", "Other"), ("Other", "A Writer"), ("The Shared Wor", "A Writer")):
            synchronize_submission(self.submission(title=title, author=author))
        self.assertEqual(PersonalTBRMatch.objects.count(), 1)

    def test_catalog_submission_can_fallback_to_manual_locked_entry(self):
        self.locked_tbr([{"title_snapshot": "Shared Work", "author_snapshot": "A. Writer"}])
        catalog, edition = self.catalog("work")
        submission = self.submission(catalog_book=catalog, catalog_edition=edition)
        synchronize_submission(submission)
        self.assertEqual(PersonalTBRMatch.objects.get().method, PersonalTBRMatch.Method.NORMALIZED_TITLE_AUTHOR)

    def test_pure_selector_fails_safe_for_ambiguous_candidates(self):
        candidates = [
            SimpleNamespace(pk=1, catalog_book_id=None, normalized_title="same", normalized_author="writer"),
            SimpleNamespace(pk=2, catalog_book_id=None, normalized_title="same", normalized_author="writer"),
        ]
        method, selected = select_candidates(
            submission_catalog_book_id=None, normalized_title="same", normalized_author="writer", books=candidates,
        )
        self.assertEqual(method, PersonalTBRMatch.Method.NORMALIZED_TITLE_AUTHOR)
        self.assertEqual([book.pk for book in selected], [1, 2])

    def test_draft_and_confirmed_empty_tbr_are_ignored(self):
        draft = PersonalTBR.objects.create(enrollment=self.enrollment)
        PersonalTBRBook.objects.create(personal_tbr=draft, position=1, title_snapshot="Shared Work", author_snapshot="A. Writer")
        synchronize_submission(self.submission())
        self.assertFalse(PersonalTBRMatch.objects.exists())
        PersonalTBRBook.objects.filter(personal_tbr=draft).delete()
        draft.confirmed_at = timezone.now(); draft.save(update_fields=["confirmed_at"])
        synchronize_submission(self.submission())
        self.assertFalse(PersonalTBRMatch.objects.exists())

    def test_rejection_removal_withdrawal_and_reactivation_preserve_one_history_row(self):
        self.locked_tbr([{"title_snapshot": "Shared Work", "author_snapshot": "A. Writer"}])
        submission = self.submission(); synchronize_submission(submission)
        match_id = PersonalTBRMatch.objects.get().pk
        submission.status = BookSubmission.Status.REJECTED; submission.approved_pages = None; submission.save(); synchronize_submission(submission)
        self.assertFalse(PersonalTBRMatch.objects.get().is_qualifying)
        submission.status = BookSubmission.Status.APPROVED; submission.approved_pages = 300; submission.save(); synchronize_submission(submission)
        self.assertTrue(PersonalTBRMatch.objects.get().is_qualifying)
        self.enrollment.is_active = False; self.enrollment.save(update_fields=["is_active"]); synchronize_reader(month=self.month, participant=self.reader)
        self.assertFalse(PersonalTBRMatch.objects.get().is_qualifying)
        self.enrollment.is_active = True; self.enrollment.save(update_fields=["is_active"]); synchronize_reader(month=self.month, participant=self.reader)
        match = PersonalTBRMatch.objects.get(); self.assertEqual(match.pk, match_id); self.assertTrue(match.is_qualifying)
        submission.is_removed = True; submission.save(update_fields=["is_removed"]); synchronize_submission(submission)
        self.assertFalse(PersonalTBRMatch.objects.get().is_qualifying)
        self.assertEqual(PersonalTBRMatch.objects.count(), 1)

    def test_multiple_submissions_allow_only_one_current_qualifier_and_can_promote_next(self):
        self.locked_tbr([{"title_snapshot": "Shared Work", "author_snapshot": "A. Writer"}])
        first = self.submission(); second = self.submission()
        synchronize_reader(month=self.month, participant=self.reader)
        self.assertEqual(PersonalTBRMatch.objects.filter(is_qualifying=True).count(), 1)
        self.assertEqual(PersonalTBRMatch.objects.count(), 2)
        first.is_removed = True; first.save(update_fields=["is_removed"])
        synchronize_reader(month=self.month, participant=self.reader)
        self.assertTrue(PersonalTBRMatch.objects.get(submission=second).is_qualifying)

    def make_pending(self):
        self.locked_tbr([{"title_snapshot": "Shared Work", "author_snapshot": "A. Writer"}])
        submission = self.submission()
        book = self.enrollment.personal_tbr.books.get()
        return PersonalTBRMatch.objects.create(
            personal_tbr_book=book, month=self.month, participant=self.reader, submission=submission,
            method=PersonalTBRMatch.Method.NORMALIZED_TITLE_AUTHOR,
            status=PersonalTBRMatch.Status.PENDING_REVIEW, is_qualifying=False,
            tbr_title_snapshot=book.title_snapshot, tbr_author_snapshot=book.author_snapshot,
            submission_title_snapshot=submission.title, submission_author_snapshot=submission.author,
            evidence_summary="Ambiguous normalized identity.",
        )

    def test_host_floater_team_leader_and_platform_owner_authority_with_safe_audit(self):
        match = self.make_pending()
        for user, decision in (
            (self.host_user, PersonalTBRMatch.Status.REJECTED),
            (self.floater_user, PersonalTBRMatch.Status.REJECTED),
            (self.leader_user, PersonalTBRMatch.Status.CONFIRMED),
            (self.root, PersonalTBRMatch.Status.REJECTED),
        ):
            adjudicate_match(match=match, actor=user, decision=decision)
            match.refresh_from_db()
            self.assertEqual((match.status, match.reviewer), (decision, user))
            self.assertIsNotNone(match.decided_at)
            PersonalTBRMatch.objects.filter(pk=match.pk).update(
                status=PersonalTBRMatch.Status.PENDING_REVIEW, is_qualifying=False,
                reviewer=None, decided_at=None,
            )
            match.refresh_from_db()
        summaries = " ".join(AuditEvent.objects.filter(action__startswith="personal_tbr.match_").values_list("summary", flat=True))
        self.assertNotIn("token", summaries.casefold())

    def test_unauthorized_reader_group_owner_and_other_team_are_denied_and_hidden(self):
        match = self.make_pending()
        other_submission = self.submission(participant=self.other)
        other_tbr, other_books = self.locked_tbr([{"title_snapshot": "Shared Work", "author_snapshot": "A. Writer"}], enrollment=self.other_enrollment)
        other_match = PersonalTBRMatch.objects.create(
            personal_tbr_book=other_books[0], month=self.month, participant=self.other, submission=other_submission,
            method=PersonalTBRMatch.Method.NORMALIZED_TITLE_AUTHOR, status=PersonalTBRMatch.Status.PENDING_REVIEW,
            tbr_title_snapshot="Shared Work", tbr_author_snapshot="A. Writer",
            submission_title_snapshot="Shared Work", submission_author_snapshot="A. Writer",
        )
        for user in (self.reader_user, self.owner_user):
            with self.assertRaises(ValidationError):
                adjudicate_match(match=match, actor=user, decision=PersonalTBRMatch.Status.CONFIRMED)
        self.client.force_login(self.leader_user)
        url = reverse("personal-tbr-match-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        page = self.client.get(url)
        self.assertContains(page, match.submission_title_snapshot)
        self.assertContains(page, "Qualifying approved completions earn the configured Personal TBR bonuses.")
        self.assertNotContains(page, "Personal TBR rewards are not active yet.")
        self.assertNotContains(page, other_match.participant.display_name)
        review_url = reverse("personal-tbr-match-review", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": other_match.pk, "decision": "confirm"})
        self.assertEqual(self.client.post(review_url).status_code, 403)
        self.assertNotContains(page, "Hardcover connection")
        self.assertNotContains(page, "registration answer")

    def test_match_truth_changes_no_scoring_or_provenance(self):
        self.locked_tbr([{"title_snapshot": "Shared Work", "author_snapshot": "A. Writer"}])
        submission = self.submission(); before = (submission.bonus_pages, submission.final_scored_pages)
        synchronize_submission(submission); submission.refresh_from_db()
        self.assertEqual((submission.bonus_pages, submission.final_scored_pages), before)
        self.assertFalse(ModifierProvenance.objects.filter(source_type__in=["tbr_book", "tbr_completion"]).exists())
