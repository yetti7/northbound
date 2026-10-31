from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase
from django.urls import reverse

from .integrations.secrets import encrypt_token
from .models import (
    CatalogBook,
    CatalogEdition,
    ChallengeMonth,
    ChallengeSignupAnswer,
    ChallengeSignupQuestion,
    HardcoverConnection,
    Membership,
    ModifierProvenance,
    MonthEnrollment,
    PersonalTBR,
    ReaderHardcoverConnection,
    ReadingGroup,
    UserProfile,
)


class PersonalTbrRegistrationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.reader_user = User.objects.create_user("tbr-registration-reader", password="password")
        self.other_user = User.objects.create_user("tbr-registration-other", password="password")
        UserProfile.objects.create(user=self.reader_user)
        UserProfile.objects.create(user=self.other_user)
        self.group = ReadingGroup.objects.create(name="TBR Registration", slug="tbr-registration")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Reader")
        self.other = Membership.objects.create(group=self.group, user=self.other_user, display_name="Other")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="TBR Registration Challenge",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            status=ChallengeMonth.Status.UPCOMING,
            registration_is_open=True,
            tbr_enabled=True,
            tbr_book_bonus_pages=25,
            tbr_completion_bonus_pages=200,
        )
        self.register_url = reverse("challenge-register", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.confirm_url = reverse("challenge-register-confirm", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.catalog_url = reverse("personal-tbr-catalog", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.client.force_login(self.reader_user)

    def payload(self, books=(), **answers):
        data = {
            "tbr-books-TOTAL_FORMS": "9",
            "tbr-books-INITIAL_FORMS": "0",
            "tbr-books-MIN_NUM_FORMS": "0",
            "tbr-books-MAX_NUM_FORMS": "9",
            **answers,
        }
        for index in range(9):
            book = books[index] if index < len(books) else {}
            data.update({
                f"tbr-books-{index}-catalog_selection": book.get("catalog_selection", ""),
                f"tbr-books-{index}-title_snapshot": book.get("title", ""),
                f"tbr-books-{index}-author_snapshot": book.get("author", ""),
                f"tbr-books-{index}-page_count_snapshot": book.get("pages", ""),
                f"tbr-books-{index}-ORDER": str(index + 1),
                f"tbr-books-{index}-DELETE": "",
            })
        return data

    def stage(self, books=(), **answers):
        return self.client.post(self.register_url, self.payload(books, **answers))

    def finalize(self):
        return self.client.post(self.confirm_url)

    def test_disabled_flow_has_no_tbr_ui_warning_or_record(self):
        self.month.tbr_enabled = False
        self.month.save(update_fields=["tbr_enabled"])
        page = self.client.get(self.register_url)
        self.assertNotContains(page, "Personal TBR")
        step_one = self.client.post(self.register_url, {})
        self.assertContains(step_one, "Registration Summary")
        self.assertNotContains(step_one, "Personal TBR")
        self.assertFalse(MonthEnrollment.objects.exists())
        self.assertRedirects(self.finalize(), self.month.get_absolute_url())
        self.assertFalse(PersonalTBR.objects.exists())

    def test_step_one_validates_questions_without_enrolling_and_final_persists(self):
        question = ChallengeSignupQuestion.objects.create(
            month=self.month, wording="Favorite number", question_type=ChallengeSignupQuestion.QuestionType.NUMBER,
            is_required=True, position=1,
        )
        missing = self.stage()
        self.assertContains(missing, "This field is required")
        self.assertFalse(MonthEnrollment.objects.exists())
        confirmation = self.client.post(
            self.register_url, self.payload([], **{f"question_{question.pk}": "42"})
        )
        self.assertContains(confirmation, "Registration Summary")
        self.assertFalse(MonthEnrollment.objects.exists())
        self.assertRedirects(self.finalize(), self.month.get_absolute_url())
        enrollment = MonthEnrollment.objects.get()
        self.assertEqual(ChallengeSignupAnswer.objects.get(enrollment=enrollment, question=question).value, "42")
        self.assertIsNotNone(enrollment.personal_tbr.confirmed_at)
        self.assertNotIn(f"challenge-registration-draft:{self.reader_user.pk}:{self.month.pk}", self.client.session)

    def test_zero_three_and_nine_book_warning_copy(self):
        zero = self.stage()
        self.assertContains(zero, "haven&#x27;t added any Personal TBR books")
        three = self.stage([
            {"title": f"Book {index}", "author": "Author"} for index in range(1, 4)
        ])
        self.assertContains(three, "3 / 9 Personal TBR books")
        self.assertContains(three, "may still earn the per-book TBR bonus")
        self.assertContains(three, "not be eligible for the full 9-book completion bonus")
        nine = self.stage([{"title": f"Book {index}", "author": "Author"} for index in range(1, 10)])
        self.assertContains(nine, "9 / 9 Personal TBR books")
        self.assertNotContains(nine, "not be eligible for the full 9-book completion bonus")

    def test_bonus_copy_does_not_promise_zero_value_rewards(self):
        self.month.tbr_book_bonus_pages = 0
        self.month.tbr_completion_bonus_pages = 0
        self.month.save(update_fields=["tbr_book_bonus_pages", "tbr_completion_bonus_pages"])
        response = self.stage([{"title": "One", "author": "Writer"}])
        self.assertNotContains(response, "Each qualifying TBR book is worth")
        self.assertNotContains(response, "additional +")
        self.assertNotContains(response, "may still earn the per-book TBR bonus")

    def test_manual_snapshot_page_count_normalization_and_lock(self):
        response = self.stage([{"title": " Manual—Book! ", "author": " A. Writer ", "pages": "321"}])
        self.assertContains(response, "Manual—Book!")
        self.finalize()
        book = PersonalTBR.objects.get().books.get()
        self.assertEqual(book.title_snapshot, "Manual—Book!")
        self.assertEqual(book.author_snapshot, "A. Writer")
        self.assertEqual(book.page_count_snapshot, 321)
        self.assertEqual(book.normalized_title, "manual book")
        self.assertIsNotNone(book.personal_tbr.confirmed_at)
        self.assertFalse(ModifierProvenance.objects.filter(source_type__in=["tbr_book", "tbr_completion"]).exists())

    def test_empty_title_author_tenth_and_duplicates_are_rejected(self):
        missing = self.stage([{"title": "Title", "author": ""}])
        self.assertContains(missing, "Enter an author")
        duplicate = self.stage([
            {"title": "Same—Book", "author": "Writer"},
            {"title": "same book", "author": "writer"},
        ])
        self.assertContains(duplicate, "same title and author")
        ten = self.payload([{"title": f"Book {i}", "author": "Writer"} for i in range(10)])
        ten["tbr-books-TOTAL_FORMS"] = "10"
        ten.update({
            "tbr-books-9-title_snapshot": "Book 9", "tbr-books-9-author_snapshot": "Writer",
            "tbr-books-9-page_count_snapshot": "", "tbr-books-9-catalog_selection": "",
            "tbr-books-9-ORDER": "10", "tbr-books-9-DELETE": "",
        })
        response = self.client.post(self.register_url, ten)
        self.assertContains(response, "at most 9 forms")

    def test_title_only_similarity_is_allowed(self):
        response = self.stage([
            {"title": "Shared", "author": "First"},
            {"title": "Shared", "author": "Second"},
        ])
        self.assertContains(response, "Registration Summary")

    def test_reader_catalog_search_uses_only_reader_token(self):
        ReaderHardcoverConnection.objects.create(
            user=self.reader_user, encrypted_token=encrypt_token("reader-token"), is_valid=True
        )
        HardcoverConnection.objects.create(group=self.group, encrypted_token=encrypt_token("group-token"), is_valid=True)
        with patch("core.views.search_books", return_value=([{"book_id": "1", "title": "Found"}], False)) as search:
            response = self.client.post(self.catalog_url, {"action": "search", "query": "Found"})
        self.assertEqual(response.status_code, 200)
        search.assert_called_once_with("reader-token", "Found")
        self.assertNotContains(response, "reader-token")
        self.assertNotContains(response, "group-token")

    def test_smart_plain_inputs_search_with_only_the_reader_token(self):
        ReaderHardcoverConnection.objects.create(
            user=self.reader_user, encrypted_token=encrypt_token("reader-token"), is_valid=True
        )
        HardcoverConnection.objects.create(group=self.group, encrypted_token=encrypt_token("group-token"), is_valid=True)
        for value in ("A Book Title", "An Author Name", "9781234567890"):
            with self.subTest(value=value), patch(
                "core.views.search_books", return_value=([{"book_id": "1", "title": "Found"}], False)
            ) as search:
                response = self.client.post(self.catalog_url, {"action": "smart", "input": value})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["lookup_type"], "search")
            search.assert_called_once_with("reader-token", value)

    def test_smart_book_link_uses_shared_resolver_and_requests_edition_selection(self):
        ReaderHardcoverConnection.objects.create(
            user=self.reader_user, encrypted_token=encrypt_token("reader-token"), is_valid=True
        )
        link = "https://hardcover.app/books/example-book"
        resolved = {"book_id": "work", "title": "Example", "edition_required": True}
        with patch("core.views.lookup_hardcover_url", return_value=(resolved, False)) as lookup:
            response = self.client.post(self.catalog_url, {"action": "smart", "input": link})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["lookup_type"], "book")
        self.assertTrue(response.json()["result"]["edition_required"])
        lookup.assert_called_once_with("reader-token", link)

    def test_smart_edition_link_resolves_exact_edition_for_intentional_selection(self):
        ReaderHardcoverConnection.objects.create(
            user=self.reader_user, encrypted_token=encrypt_token("reader-token"), is_valid=True
        )
        catalog = CatalogBook.objects.create(
            provider="hardcover", provider_book_id="work", title="Exact Book", author="Exact Author"
        )
        edition = CatalogEdition.objects.create(
            book=catalog, provider="hardcover", provider_edition_id="30407787", page_count=321
        )
        selected = {
            "edition_id": "30407787", "book_id": "work", "title": "Exact Book",
            "author": "Exact Author", "pages": 321, "format": "Hardcover",
        }
        link = "https://hardcover.app/books/exact-book/editions/30407787"
        with patch("core.views.lookup_hardcover_url", return_value=(selected.copy(), False)) as lookup, patch(
            "core.views.resolve_scoring_edition", return_value=(selected.copy(), "selected")
        ), patch("core.views.lookup_edition", return_value=(selected.copy(), False)):
            response = self.client.post(self.catalog_url, {"action": "smart", "input": link})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["lookup_type"], "edition")
        self.assertEqual(payload["result"]["edition_id"], "30407787")
        self.assertTrue(payload["result"]["catalog_selection"])
        lookup.assert_called_once_with("reader-token", link)
        self.assertEqual(edition.provider_edition_id, payload["result"]["edition_id"])

    def test_smart_invalid_or_unsupported_urls_fail_without_becoming_searches(self):
        ReaderHardcoverConnection.objects.create(
            user=self.reader_user, encrypted_token=encrypt_token("reader-token"), is_valid=True
        )
        for value in ("https://hardcover.app/not-books/example", "https://example.com/books/example"):
            with self.subTest(value=value), patch("core.views.search_books") as search:
                response = self.client.post(self.catalog_url, {"action": "smart", "input": value})
            self.assertEqual(response.status_code, 400)
            self.assertIn("hardcover", response.json()["message"].casefold())
            search.assert_not_called()

    def test_group_connection_never_enables_reader_search_and_manual_remains_available(self):
        HardcoverConnection.objects.create(group=self.group, encrypted_token=encrypt_token("group-token"), is_valid=True)
        page = self.client.get(self.register_url)
        self.assertContains(page, "Enter manually")
        self.assertContains(page, "You can still enter every book manually below")
        response = self.client.post(self.catalog_url, {"action": "smart", "input": "Book"})
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, "group-token", status_code=503)
        self.assertContains(self.stage([{"title": "Manual", "author": "Writer"}]), "Registration Summary")

    def test_registration_copy_flattens_search_and_makes_both_entry_methods_explicit(self):
        page = self.client.get(self.register_url)
        self.assertContains(page, "Optional. Add up to 9 books.")
        self.assertContains(page, "You can edit your list until you confirm your registration.")
        self.assertContains(page, "0 / 9 books selected")
        self.assertContains(page, "+25</strong> bonus pages per qualifying TBR book")
        self.assertContains(page, "+200</strong> bonus pages for completing all 9")
        self.assertContains(page, "Search Hardcover")
        self.assertContains(page, "Enter manually")
        self.assertContains(page, "Use the Title and Author fields below. Page Count and Cover URL are optional.")
        self.assertNotContains(page, 'class="form-card" data-tbr-catalog')
        ReaderHardcoverConnection.objects.create(
            user=self.reader_user, encrypted_token=encrypt_token("reader-token"), is_valid=True
        )
        connected = self.client.get(self.register_url)
        self.assertContains(connected, "Search Hardcover for this book")
        self.assertContains(connected, "Search by title, author, ISBN, or paste a Hardcover link.")
        self.assertNotContains(connected, "Search here to add a new book automatically")
        self.assertContains(connected, "The selected edition will be added as a new Personal TBR book")
        self.assertContains(connected, "Cancel row replacement")
        self.assertContains(connected, "personal_tbr_registration_helpers.js")
        self.assertContains(connected, "Title, Author, ISBN, or Hardcover Link")
        self.assertContains(connected, "Exact Hardcover edition found. Select Use Edition to continue.")

    def test_catalog_selection_persists_canonical_snapshot_and_tampering_fails(self):
        catalog = CatalogBook.objects.create(
            provider="hardcover", provider_book_id="work", title="Catalog Title", author="Catalog Author",
            cover_url="https://example.com/cover.jpg", source_url="https://example.com/work",
        )
        edition = CatalogEdition.objects.create(
            book=catalog, provider="hardcover", provider_edition_id="edition", page_count=444,
            source_url="https://example.com/edition",
        )
        selection = signing.dumps({"selected": edition.pk, "scoring": edition.pk}, salt="northbound.personal-tbr-selection")
        self.stage([{"title": "Tampered", "author": "Wrong", "catalog_selection": selection}])
        self.finalize()
        book = PersonalTBR.objects.get().books.get()
        self.assertEqual(book.catalog_book, catalog)
        self.assertEqual(book.catalog_edition, edition)
        self.assertEqual((book.title_snapshot, book.author_snapshot, book.page_count_snapshot), ("Catalog Title", "Catalog Author", 444))
        MonthEnrollment.objects.all().delete()
        bad = self.stage([{"title": "Bad", "author": "Bad", "catalog_selection": f"{selection}tampered"}])
        self.assertContains(bad, "invalid or expired")

    def test_final_confirmation_revalidates_registration_membership_and_tbr_setting(self):
        self.stage([{"title": "Draft", "author": "Writer"}])
        self.month.registration_is_open = False
        self.month.save(update_fields=["registration_is_open"])
        response = self.finalize()
        self.assertRedirects(response, self.register_url, fetch_redirect_response=False)
        self.assertFalse(MonthEnrollment.objects.exists())
        self.month.registration_is_open = True
        self.month.save(update_fields=["registration_is_open"])
        self.stage([{"title": "Draft", "author": "Writer"}])
        self.month.tbr_enabled = False
        self.month.save(update_fields=["tbr_enabled"])
        self.finalize()
        self.assertFalse(MonthEnrollment.objects.exists())
        self.month.tbr_enabled = True
        self.month.save(update_fields=["tbr_enabled"])
        self.stage([{"title": "Draft", "author": "Writer"}])
        self.reader.is_active = False
        self.reader.save(update_fields=["is_active"])
        self.finalize()
        self.assertFalse(MonthEnrollment.objects.exists())

    def test_tbr_failure_rolls_back_enrollment_answers_and_children(self):
        question = ChallengeSignupQuestion.objects.create(
            month=self.month, wording="Text", question_type=ChallengeSignupQuestion.QuestionType.SHORT_TEXT,
            is_required=True, position=1,
        )
        self.client.post(self.register_url, self.payload(
            [{"title": "Book", "author": "Writer"}], **{f"question_{question.pk}": "Answer"}
        ))
        with patch("core.views.confirm_personal_tbr", side_effect=RuntimeError("forced failure")):
            with self.assertRaises(RuntimeError):
                self.finalize()
        self.assertFalse(MonthEnrollment.objects.exists())
        self.assertFalse(ChallengeSignupAnswer.objects.exists())
        self.assertFalse(PersonalTBR.objects.exists())

    def test_withdrawn_locked_tbr_is_read_only_and_reused_on_reregistration(self):
        self.stage([{"title": "Locked", "author": "Writer"}])
        self.finalize()
        enrollment = MonthEnrollment.objects.get()
        tbr = enrollment.personal_tbr
        enrollment.is_active = False
        enrollment.inactive_reason = MonthEnrollment.InactiveReason.WITHDRAWN
        enrollment.save(update_fields=["is_active", "inactive_reason"])
        page = self.client.get(self.register_url)
        self.assertContains(page, "already confirmed and locked")
        self.assertNotContains(page, "Add Book")
        confirmation = self.client.post(self.register_url, {})
        self.assertContains(confirmation, "previously confirmed Personal TBR")
        self.finalize()
        enrollment.refresh_from_db()
        self.assertTrue(enrollment.is_active)
        self.assertEqual(enrollment.personal_tbr.pk, tbr.pk)
        self.assertEqual(enrollment.personal_tbr.books.get().title_snapshot, "Locked")

    def test_session_draft_is_user_and_month_scoped_and_contains_no_token(self):
        secret = "reader-secret-token"
        ReaderHardcoverConnection.objects.create(user=self.reader_user, encrypted_token=encrypt_token(secret), is_valid=True)
        self.stage([{"title": "Private Draft", "author": "Writer"}])
        serialized = str(dict(self.client.session))
        self.assertNotIn(secret, serialized)
        self.client.force_login(self.other_user)
        other_page = self.client.get(self.register_url)
        self.assertNotContains(other_page, "Private Draft")
