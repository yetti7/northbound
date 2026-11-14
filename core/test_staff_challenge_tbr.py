from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    AuditEvent, BookSubmission, CatalogBook, CatalogEdition, ChallengeMonth,
    ChallengeSignupQuestion, ChallengeStaffAssignment, HardcoverConnection,
    HardcoverSyncOutbox, Membership, ModifierProvenance, MonthEnrollment,
    PersonalTBR, PersonalTBRCompletionAward, PersonalTBRMatch, ProgressCheckpoint,
    ProgressCheckpointResult, ReadingGroup,
    Team, TeamAssignment, UserProfile,
)
from .permissions import can_manage_challenge_tbr
from .personal_tbr import (
    confirm_personal_tbr, replace_draft_personal_tbr,
    save_staff_challenge_tbr, staff_tbr_version,
)
from .personal_tbr_matching import synchronize_submission
from .reader_hardcover import ReaderHardcoverUnavailable


class StaffChallengeTbrTests(TestCase):
    def setUp(self):
        self.group = ReadingGroup.objects.create(name="Staff TBR", slug="staff-tbr")
        self.staff_user = get_user_model().objects.create_user("staff")
        self.reader_user = get_user_model().objects.create_user("reader")
        for user in (self.staff_user, self.reader_user):
            UserProfile.objects.create(user=user)
        self.staff = Membership.objects.create(group=self.group, user=self.staff_user, display_name="Staff", role=Membership.Role.OWNER)
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Reader")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="Challenge", starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 30),
            status=ChallengeMonth.Status.ACTIVE, tbr_enabled=True, tbr_book_bonus_pages=25,
            tbr_completion_bonus_pages=200,
        )
        self.enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        self.url = reverse("staff-challenge-tbr", args=[self.group.slug, self.month.pk, self.reader.pk])
        self.catalog_url = reverse("staff-challenge-tbr-catalog", args=[self.group.slug, self.month.pk, self.reader.pk])
        self.client.force_login(self.staff_user)

    def books(self, count=9):
        return [{"position": i, "title_snapshot": f"Book {i}", "author_snapshot": "Author"} for i in range(1, count + 1)]

    def save(self, books, complete=False):
        tbr = PersonalTBR.objects.filter(enrollment=self.enrollment).first()
        return save_staff_challenge_tbr(enrollment=self.enrollment, actor=self.staff_user, books=books,
                                        complete=complete, version=staff_tbr_version(tbr))

    def payload(self, books, action="save"):
        response = self.client.get(self.url)
        data = {"version": response.context["version"], "action": action,
                "tbr-books-TOTAL_FORMS": "9", "tbr-books-INITIAL_FORMS": str(len(response.context["tbr_formset"].initial)),
                "tbr-books-MAX_NUM_FORMS": "9", "tbr-books-MIN_NUM_FORMS": "0"}
        for i in range(9):
            for key, value in (books[i] if i < len(books) else {}).items():
                data[f"tbr-books-{i}-{key}"] = value
            data[f"tbr-books-{i}-ORDER"] = i + 1
        return data

    def test_owner_and_moderator_access(self):
        for role in (Membership.Role.OWNER, Membership.Role.MODERATOR):
            self.staff.role = role; self.staff.save()
            self.assertTrue(can_manage_challenge_tbr(self.staff_user, self.enrollment))
            self.assertContains(self.client.get(self.url), "Manage TBR")

    def test_host_and_floater_are_challenge_scoped(self):
        self.staff.role = Membership.Role.MEMBER; self.staff.save()
        other_month = ChallengeMonth.objects.create(group=self.group, name="Other", starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 31))
        other_enrollment = MonthEnrollment.objects.create(month=other_month, participant=self.reader)
        for role in (ChallengeStaffAssignment.Role.HOST, ChallengeStaffAssignment.Role.FLOATER):
            assignment = ChallengeStaffAssignment.objects.create(month=self.month, membership=self.staff, role=role)
            self.assertTrue(can_manage_challenge_tbr(self.staff_user, self.enrollment))
            self.assertFalse(can_manage_challenge_tbr(self.staff_user, other_enrollment))
            assignment.delete()

    def test_team_leader_only_current_own_team(self):
        self.staff.role = Membership.Role.MEMBER; self.staff.save()
        team = Team.objects.create(month=self.month, name="Team")
        MonthEnrollment.objects.create(month=self.month, participant=self.staff)
        TeamAssignment.objects.create(month=self.month, team=team, participant=self.staff)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.staff, role=ChallengeStaffAssignment.Role.TEAM_LEADER, team=team)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        assignment = TeamAssignment.objects.create(month=self.month, team=team, participant=self.reader)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        assignment.delete()
        self.assertEqual(self.client.post(self.catalog_url, {"action": "search"}).status_code, 403)

    def test_reader_and_cross_group_denied_on_get_post_catalog_and_service(self):
        self.client.force_login(self.reader_user)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {}).status_code, 403)
        self.assertEqual(self.client.post(self.catalog_url, {}).status_code, 403)
        with self.assertRaises(ValidationError):
            save_staff_challenge_tbr(enrollment=self.enrollment, actor=self.reader_user, books=[])
        other = ReadingGroup.objects.create(name="Other", slug="other")
        self.staff.group = other; self.staff.save()
        self.assertFalse(can_manage_challenge_tbr(self.staff_user, self.enrollment))

    def test_draft_and_complete_do_not_complete_registration_or_enqueue_sync(self):
        self.enrollment.is_active = False; self.enrollment.save()
        ChallengeSignupQuestion.objects.create(month=self.month, wording="Required answer", question_type=ChallengeSignupQuestion.QuestionType.SHORT_TEXT, is_required=True, position=1)
        original = MonthEnrollment.objects.filter(pk=self.enrollment.pk).values().get()
        with patch("core.hardcover_sync.enqueue_submission_sync") as enqueue:
            response = self.client.post(self.url, self.payload(self.books(2)))
            self.assertEqual(response.status_code, 302)
            tbr = PersonalTBR.objects.get(enrollment=self.enrollment)
            self.assertIsNone(tbr.confirmed_at)
            response = self.client.post(self.url, self.payload(self.books(), "complete"))
            self.assertEqual(response.status_code, 302)
            tbr.refresh_from_db(); self.assertIsNotNone(tbr.confirmed_at)
            enqueue.assert_not_called()
        self.assertEqual(original, MonthEnrollment.objects.filter(pk=self.enrollment.pk).values().get())
        self.assertFalse(HardcoverSyncOutbox.objects.exists())
        self.assertFalse(self.enrollment.signup_answers.exists())
        self.assertNotContains(self.client.get(self.url), 'value="complete"')
        self.assertContains(self.client.get(self.url), "Staff-assisted")
        self.assertEqual(AuditEvent.objects.filter(action="challenge_tbr.staff_updated").count(), 2)

    def test_completion_validation_and_duplicates(self):
        with self.assertRaisesMessage(ValidationError, "nine valid"):
            self.save(self.books(8), complete=True)
        books = self.books(); books[1]["title_snapshot"] = books[0]["title_snapshot"]
        with self.assertRaises(ValidationError):
            self.save(books)
        self.assertFalse(PersonalTBR.objects.exists())

    def test_corrections_after_reader_lock_and_reordering_unmatched_books(self):
        tbr = replace_draft_personal_tbr(enrollment=self.enrollment, books=self.books(3))
        confirm_personal_tbr(enrollment=self.enrollment)
        tbr.refresh_from_db(); locked_at = tbr.confirmed_at
        changed = self.books(3); changed[0]["title_snapshot"] = "Corrected"
        changed[1]["title_snapshot"], changed[2]["title_snapshot"] = "Book 3", "Book 2"
        self.save(changed)
        tbr.refresh_from_db(); self.assertEqual(tbr.confirmed_at, locked_at)
        self.assertEqual(list(tbr.books.values_list("title_snapshot", flat=True)), ["Corrected", "Book 3", "Book 2"])
        with self.assertRaises(ValidationError):
            replace_draft_personal_tbr(enrollment=self.enrollment, books=[])

    def test_matched_books_and_completion_award_preserved(self):
        tbr = self.save(self.books(), complete=True)
        for book in tbr.books.all():
            submission = BookSubmission.objects.create(month=self.month, participant=self.reader,
                title=book.title_snapshot, author=book.author_snapshot, book_format=BookSubmission.Format.EBOOK,
                completed_on=date(2026, 9, 10), submitted_pages=100, approved_pages=100,
                status=BookSubmission.Status.APPROVED)
            synchronize_submission(submission)
        self.assertTrue(PersonalTBRCompletionAward.objects.exists())
        models = [BookSubmission, PersonalTBRMatch, PersonalTBRCompletionAward, ModifierProvenance]
        before = [list(model.objects.values()) for model in models]
        unchanged = [{"position": book.position, "title_snapshot": book.title_snapshot,
                      "author_snapshot": book.author_snapshot} for book in tbr.books.all()]
        self.save(unchanged)
        changed = self.books(); changed[0]["title_snapshot"] = "Incorrect replacement"
        with self.assertRaisesMessage(ValidationError, "scoring history"):
            self.save(changed)
        self.assertEqual(before, [list(model.objects.values()) for model in models])

    def test_stale_editor_cannot_overwrite(self):
        tbr = self.save(self.books(1)); old_version = staff_tbr_version(tbr)
        self.save(self.books(2))
        with self.assertRaisesMessage(ValidationError, "Reload"):
            save_staff_challenge_tbr(enrollment=self.enrollment, actor=self.staff_user,
                                     books=[], version=old_version)

    @patch("core.views.search_books", return_value=([], False))
    @patch("core.views.get_reader_hardcover_token", return_value="staff-token")
    def test_search_prefers_actor_never_reader(self, token, search):
        response = self.client.post(self.catalog_url, {"action": "search", "query": "Book"})
        self.assertEqual(response.status_code, 200)
        token.assert_called_once_with(self.staff_user)
        search.assert_called_once_with("staff-token", "Book")
        self.assertFalse(HardcoverSyncOutbox.objects.exists())

    @patch("core.views.search_books", return_value=([], False))
    @patch("core.views.decrypt_token", return_value="group-token")
    @patch("core.views.get_reader_hardcover_token", side_effect=ReaderHardcoverUnavailable())
    def test_group_search_fallback(self, token, decrypt, search):
        HardcoverConnection.objects.create(group=self.group, encrypted_token="encrypted", is_valid=True)
        self.assertEqual(self.client.post(self.catalog_url, {"action": "search", "query": "Book"}).status_code, 200)
        token.assert_called_once_with(self.staff_user)
        decrypt.assert_called_once_with("encrypted")
        search.assert_called_once_with("group-token", "Book")
        self.assertFalse(HardcoverSyncOutbox.objects.exists())

    def test_manual_fallback(self):
        self.assertContains(self.client.get(self.url), "Use the Title and Author")
        self.assertEqual(self.client.post(self.catalog_url, {"action": "search", "query": "Book"}).status_code, 503)
        self.assertEqual(self.client.post(self.url, self.payload(self.books(1))).status_code, 302)

    def test_management_actions_on_list_and_profile(self):
        self.assertContains(self.client.get(reverse("month-participant-list", args=[self.group.slug, self.month.pk])), "Manage TBR")
        self.assertContains(self.client.get(reverse("challenge-participant-detail", args=[self.group.slug, self.month.pk, self.reader.pk])), "Manage TBR")
        self.client.force_login(self.reader_user)
        self.assertNotContains(self.client.get(reverse("challenge-participant-detail", args=[self.group.slug, self.month.pk, self.reader.pk])), "Manage TBR")

    def test_catalog_selection_survives_staff_noop_save(self):
        from django.core import signing
        catalog = CatalogBook.objects.create(provider="hardcover", provider_book_id="123", title="Catalog book", author="Writer")
        edition = CatalogEdition.objects.create(book=catalog, provider="hardcover", provider_edition_id="456", page_count=123)
        values = [{"position": 1, "title_snapshot": catalog.title, "author_snapshot": catalog.author,
                   "catalog_book": catalog, "catalog_edition": edition, "page_count_snapshot": 123,
                   "cover_url_snapshot": "https://example.com/cover.png"}]
        tbr = self.save(values, complete=False)
        book = tbr.books.get()
        data = self.payload([{**values[0], "existing_selection": signing.dumps({"book": book.pk}, salt="staff-tbr-existing")}])
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        retained = tbr.books.get()
        self.assertEqual((retained.pk, retained.catalog_book_id, retained.catalog_edition_id, retained.cover_url_snapshot),
                         (book.pk, catalog.pk, edition.pk, "https://example.com/cover.png"))

    def test_staff_search_rejects_write_actions(self):
        with patch("core.views.get_reader_hardcover_token", return_value="staff-token"), patch("core.hardcover_sync.enqueue_submission_sync") as enqueue:
            self.assertEqual(self.client.post(self.catalog_url, {"action": "mark_read"}).status_code, 400)
            enqueue.assert_not_called()
        self.assertFalse(HardcoverSyncOutbox.objects.exists())

    def test_correct_unmatched_book_preserves_other_match_bonus_and_checkpoint(self):
        tbr = self.save(self.books(2))
        confirm_personal_tbr(enrollment=self.enrollment)
        first = tbr.books.first()
        submission = BookSubmission.objects.create(
            month=self.month, participant=self.reader, title=first.title_snapshot, author=first.author_snapshot,
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 9, 10), submitted_pages=100,
            approved_pages=100, status=BookSubmission.Status.APPROVED,
        )
        synchronize_submission(submission)
        checkpoint = ProgressCheckpoint.objects.create(month=self.month, scheduled_at=timezone.now())
        ProgressCheckpointResult.objects.create(
            checkpoint=checkpoint, participant=self.reader, evaluated_at=timezone.now(),
            threshold_percentage=25, progress_basis="base", target_basis="fixed", target_pages=100,
            required_pages=25, progress_pages=125, outcome="met",
        )
        models = [BookSubmission, PersonalTBRMatch, ModifierProvenance, ProgressCheckpointResult]
        before = [list(model.objects.values()) for model in models]
        changed = self.books(2); changed[1]["title_snapshot"] = "Corrected second book"
        self.save(changed)
        self.assertEqual(tbr.books.first().pk, first.pk)
        self.assertEqual(before, [list(model.objects.values()) for model in models])
        for invalid in (self.books(1)[1:], [{**changed[1], "position": 1}, {**changed[0], "position": 2}]):
            with self.assertRaisesMessage(ValidationError, "scoring history"):
                self.save(invalid)

    def edit_url(self, book):
        return reverse("staff-challenge-tbr-book-edit", args=[
            self.group.slug, self.month.pk, self.reader.pk, book.pk,
        ])

    def edit_data(self, book, **changes):
        from .personal_tbr import staff_tbr_book_version

        book.refresh_from_db()
        return {"title": book.title_snapshot, "author": book.author_snapshot,
                "version": staff_tbr_book_version(book), **changes}

    def test_in_place_title_then_author_preserves_entry_and_scoring(self):
        from .models import PersonalTBRBook

        tbr = self.save(self.books(), complete=True)
        book = tbr.books.first()
        before = PersonalTBRBook.objects.values().get(pk=book.pk)
        url = self.edit_url(book)
        self.assertContains(self.client.get(self.url), url)
        self.assertContains(self.client.get(url), 'value="Book 1"')
        submission = BookSubmission.objects.create(
            month=self.month, participant=self.reader, title="Correct title", author="Correct author",
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 9, 5),
            submitted_pages=100, approved_pages=100, final_scored_pages=100,
            status=BookSubmission.Status.APPROVED,
        )
        models = (BookSubmission, PersonalTBRMatch, PersonalTBRCompletionAward, ModifierProvenance, HardcoverSyncOutbox)
        snapshots = {model: list(model.objects.order_by("pk").values()) for model in models}
        stale = self.edit_data(book, author="Old tab")
        self.assertRedirects(self.client.post(url, self.edit_data(book, title="Correct title")), self.url)
        self.assertContains(self.client.post(url, stale), "This book changed")
        self.assertRedirects(self.client.post(url, self.edit_data(book, author="Correct author")), self.url)
        self.assertEqual(PersonalTBRBook.objects.values().get(pk=book.pk), {
            **before, "title_snapshot": "Correct title", "author_snapshot": "Correct author",
            "display_title": "Correct title", "display_author": "Correct author",
            "normalized_title": "correct title", "normalized_author": "correct author",
        })
        for model, snapshot in snapshots.items():
            self.assertEqual(list(model.objects.order_by("pk").values()), snapshot)
        self.assertEqual(BookSubmission.objects.get(pk=submission.pk).final_scored_pages, 100)
        events = AuditEvent.objects.filter(action="challenge_tbr.book_edited", object_id=str(book.pk))
        self.assertEqual(events.count(), 2)
        self.assertEqual(events.first().actor, self.staff_user)
        self.assertEqual(events.first().group, self.group)
        self.assertIn('"author": "Correct author"', events.first().summary)

    def test_history_acquired_after_opening_form_blocks_edit(self):
        tbr = self.save(self.books(), complete=True)
        book = tbr.books.first()
        data = self.edit_data(book, title="Changed")
        submission = BookSubmission.objects.create(
            month=self.month, participant=self.reader, title=book.title_snapshot, author=book.author_snapshot,
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 9, 5),
            submitted_pages=100, approved_pages=100, status=BookSubmission.Status.APPROVED,
        )
        BookSubmission.objects.filter(pk=submission.pk).update(selected_tbr_book=book)
        self.assertContains(self.client.post(self.edit_url(book), data), "Reader selection history")
        BookSubmission.objects.filter(pk=submission.pk).update(selected_tbr_book=None)
        synchronize_submission(submission)
        self.assertTrue(book.matches.filter(is_qualifying=True).exists())
        self.assertNotContains(self.client.get(self.url), self.edit_url(book))
        self.assertContains(self.client.post(self.edit_url(book), data), "match, award, or Reader selection history")
        book.refresh_from_db()
        self.assertEqual(book.title_snapshot, "Book 1")
        self.assertFalse(AuditEvent.objects.filter(action="challenge_tbr.book_edited").exists())

    def test_catalog_conflict_and_duplicate_are_field_errors(self):
        catalog = CatalogBook.objects.create(provider="hardcover", provider_book_id="edit", title="Trusted", author="Author")
        edition = CatalogEdition.objects.create(book=catalog, provider="hardcover", provider_edition_id="edit")
        values = self.books(2)
        values[0].update(catalog_book=catalog, catalog_edition=edition)
        tbr = self.save(values)
        linked, manual = list(tbr.books.all())
        response = self.client.post(self.edit_url(linked), self.edit_data(linked, title="Conflicting"))
        self.assertIn("title", response.context["form"].errors)
        self.assertRedirects(self.client.post(self.edit_url(linked), self.edit_data(linked, title="Trusted")), self.url)
        linked.refresh_from_db()
        self.assertEqual((linked.catalog_book_id, linked.catalog_edition_id), (catalog.pk, edition.pk))
        response = self.client.post(self.edit_url(manual), self.edit_data(manual, title="Trusted"))
        self.assertIn("title", response.context["form"].errors)

    def test_edit_endpoint_respects_floater_team_leader_and_participating_host_scope(self):
        book = self.save(self.books(1)).books.get()
        url = self.edit_url(book)
        self.staff.role = Membership.Role.MEMBER
        self.staff.save()
        team = Team.objects.create(month=self.month, name="Readers")
        other = Team.objects.create(month=self.month, name="Other")
        TeamAssignment.objects.create(month=self.month, participant=self.reader, team=team)
        staff_team = None
        for role, assigned, allowed in (
            (ChallengeStaffAssignment.Role.FLOATER, None, True),
            (ChallengeStaffAssignment.Role.TEAM_LEADER, team, True),
            (ChallengeStaffAssignment.Role.TEAM_LEADER, other, False),
            (ChallengeStaffAssignment.Role.HOST, None, False),
        ):
            with self.subTest(role=role, allowed=allowed):
                if role != ChallengeStaffAssignment.Role.FLOATER:
                    if staff_team is None:
                        MonthEnrollment.objects.create(month=self.month, participant=self.staff)
                        staff_team = TeamAssignment.objects.create(month=self.month, participant=self.staff, team=assigned)
                    else:
                        TeamAssignment.objects.filter(pk=staff_team.pk).update(team=assigned or other)
                assignment = ChallengeStaffAssignment.objects.create(
                    month=self.month, membership=self.staff, role=role, team=assigned,
                )
                self.assertEqual(self.client.get(url).status_code, 200 if allowed else 403)
                self.assertEqual(self.client.post(url, self.edit_data(book)).status_code, 302 if allowed else 403)
                assignment.delete()
        foreign_group = ReadingGroup.objects.create(name="Other group", slug="other-tbr-group")
        outsider = get_user_model().objects.create_user("foreign-tbr-owner")
        Membership.objects.create(group=foreign_group, user=outsider, role=Membership.Role.OWNER)
        for user in (outsider, self.reader_user):
            self.client.force_login(user)
            self.assertEqual(self.client.post(url, self.edit_data(book)).status_code, 403)
