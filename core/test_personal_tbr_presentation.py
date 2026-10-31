from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    BookSubmission, CatalogBook, CatalogEdition, ChallengeMonth, ChallengeSignupAnswer, ChallengeSignupQuestion,
    Membership, MonthEnrollment, PersonalTBR, PersonalTBRBook, PersonalTBRCompletionAward,
    PersonalTBRMatch, ReaderHardcoverConnection, ReadingGroup,
)


class PersonalTbrReaderPresentationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.reader_user = User.objects.create_user("tbr-presentation-reader")
        self.other_user = User.objects.create_user("tbr-presentation-other")
        self.group = ReadingGroup.objects.create(name="TBR Presentation", slug="tbr-presentation")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Reader")
        self.other = Membership.objects.create(group=self.group, user=self.other_user, display_name="Other")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="TBR Presentation Challenge", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.ACTIVE,
            tbr_enabled=True, tbr_book_bonus_pages=50, tbr_completion_bonus_pages=1000,
        )
        self.enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        self.other_enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.other)
        self.tbr, self.books = self.locked_tbr(self.enrollment, "Reader", 3)
        self.catalog_book = CatalogBook.objects.create(
            provider="hardcover", provider_book_id="tbr-work", title="Reader Book 1", author="Reader Author 1",
            source_url="https://hardcover.app/books/reader-book-one",
        )
        self.catalog_edition = CatalogEdition.objects.create(
            book=self.catalog_book, provider="hardcover", provider_edition_id="tbr-edition",
            source_url="https://hardcover.app/books/reader-book-one/editions/tbr-edition",
        )
        PersonalTBRBook.objects.filter(pk=self.books[0].pk).update(
            catalog_book=self.catalog_book, catalog_edition=self.catalog_edition,
        )
        self.books[0].refresh_from_db()
        self.other_tbr, self.other_books = self.locked_tbr(self.other_enrollment, "Private Other", 1)
        self.url = reverse("personal-tbr-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})

    def locked_tbr(self, enrollment, prefix, count):
        tbr = PersonalTBR.objects.create(enrollment=enrollment)
        books = [PersonalTBRBook.objects.create(
            personal_tbr=tbr, position=position,
            title_snapshot=f"{prefix} Book {position}", author_snapshot=f"{prefix} Author {position}",
            page_count_snapshot=100 + position,
            cover_url_snapshot=f"https://example.com/{prefix.lower().replace(' ', '-')}-{position}.jpg",
        ) for position in range(1, count + 1)]
        tbr.confirmed_at = timezone.now(); tbr.save(update_fields=["confirmed_at"])
        return tbr, books

    def match(self, book, *, status, qualifying=False):
        submission = BookSubmission.objects.create(
            month=self.month, participant=self.reader, title=book.title_snapshot, author=book.author_snapshot,
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 10),
            submitted_pages=book.page_count_snapshot,
            approved_pages=book.page_count_snapshot if status != PersonalTBRMatch.Status.PENDING_REVIEW else None,
            status=BookSubmission.Status.PENDING if status == PersonalTBRMatch.Status.PENDING_REVIEW else BookSubmission.Status.APPROVED,
        )
        return PersonalTBRMatch.objects.create(
            personal_tbr_book=book, month=self.month, participant=self.reader, submission=submission,
            method=PersonalTBRMatch.Method.NORMALIZED_TITLE_AUTHOR, status=status, is_qualifying=qualifying,
            tbr_title_snapshot=book.title_snapshot, tbr_author_snapshot=book.author_snapshot,
            submission_title_snapshot=submission.title, submission_author_snapshot=submission.author,
        )

    def test_reader_sees_only_own_locked_ordered_tbr_with_details_and_no_edit_controls(self):
        self.client.force_login(self.reader_user)
        response = self.client.get(self.url)
        self.assertContains(response, "Personal TBR")
        self.assertContains(response, "confirmed Challenge TBR is permanently locked")
        self.assertContains(response, "0 / 3 completed")
        self.assertContains(response, "Reader Book 1")
        self.assertContains(response, "Reader Author 1")
        self.assertContains(response, "101 pages")
        self.assertContains(response, "+50 bonus pages per qualifying TBR book")
        self.assertNotContains(response, "Private Other Book")
        self.assertNotContains(response, "Edit")
        self.assertNotContains(response, "Remove")
        self.assertEqual([book.position for book in response.context["books"]], [1, 2, 3])

    def test_only_catalog_backed_tbr_book_exposes_hardcover_destination(self):
        self.client.force_login(self.reader_user)
        response = self.client.get(self.url)
        self.assertContains(response, self.catalog_edition.hardcover_url)
        self.assertContains(response, "View on Hardcover")
        self.assertEqual(response.content.decode().count("View on Hardcover"), 1)
        self.assertNotContains(response, "https://hardcover.app/books/reader-book-2")

    def test_other_reader_gets_only_their_own_page_and_cannot_see_reader_tbr(self):
        self.client.force_login(self.other_user)
        response = self.client.get(self.url)
        self.assertContains(response, "Private Other Book 1")
        self.assertNotContains(response, "Reader Book 1")
        self.assertContains(response, "0 / 1 completed")

    def test_match_truth_drives_completed_pending_and_rejected_presentation(self):
        self.match(self.books[0], status=PersonalTBRMatch.Status.CONFIRMED, qualifying=True)
        self.match(self.books[1], status=PersonalTBRMatch.Status.PENDING_REVIEW)
        self.match(self.books[2], status=PersonalTBRMatch.Status.REJECTED)
        self.client.force_login(self.reader_user)
        response = self.client.get(self.url)
        self.assertContains(response, "1 / 3 completed")
        self.assertEqual(response.content.decode().count("book-completion-overlay"), 1)
        self.assertContains(response, "images/completed-stamp.png")
        states = [book.reader_state for book in response.context["books"]]
        self.assertEqual(states, ["completed", "pending", "not_completed"])
        self.assertContains(response, ">Completed<")
        self.assertContains(response, ">Pending<")
        self.assertNotContains(response, ">Not completed<")
        self.assertContains(response, "101 pages · <strong>+50 bonus</strong>")
        self.assertNotContains(response, "bonus pages when completed")
        self.assertNotContains(response, "evidence_summary")
        detail = self.client.get(self.month.get_absolute_url())
        self.assertContains(detail, "1 / 3 completed")
        self.assertEqual(detail.content.decode().count("book-completion-overlay"), 1)

    def test_challenge_detail_entry_requires_enabled_confirmed_reader_owned_tbr(self):
        self.client.force_login(self.reader_user)
        response = self.client.get(self.month.get_absolute_url())
        self.assertContains(response, "Personal TBR")
        self.assertContains(response, "0 / 3 completed")
        self.assertContains(response, self.url)
        self.client.force_login(self.other_user)
        other = self.client.get(self.month.get_absolute_url())
        self.assertContains(other, "0 / 1 completed")
        self.assertNotContains(other, "Reader Book 1")
        self.month.tbr_enabled = False; self.month.save(update_fields=["tbr_enabled"])
        disabled = self.client.get(self.month.get_absolute_url())
        self.assertContains(disabled, "Personal TBR")
        self.assertContains(disabled, "Not enabled for this Challenge.")
        self.assertNotContains(disabled, self.url)
        self.month.tbr_enabled = True; self.month.save(update_fields=["tbr_enabled"])
        PersonalTBR.objects.filter(pk=self.other_tbr.pk).update(confirmed_at=None)
        unconfirmed = self.client.get(self.month.get_absolute_url())
        self.assertNotContains(unconfirmed, self.url)
        self.assertNotContains(unconfirmed, "Reader Book 1")

    def test_partial_tbr_never_advertises_full_nine_incentive(self):
        self.client.force_login(self.reader_user)
        response = self.client.get(self.url)
        self.assertNotContains(response, "Complete all 9")
        self.assertNotContains(response, "+1000 bonus pages")

    def test_exact_nine_shows_positive_completion_incentive_and_qualifying_award_copy(self):
        PersonalTBR.objects.filter(pk=self.tbr.pk).update(confirmed_at=None)
        self.tbr.refresh_from_db()
        for position in range(4, 10):
            PersonalTBRBook.objects.create(
                personal_tbr=self.tbr, position=position, title_snapshot=f"Reader Book {position}",
                author_snapshot=f"Reader Author {position}",
            )
        self.tbr.confirmed_at = timezone.now(); self.tbr.save(update_fields=["confirmed_at"])
        self.client.force_login(self.reader_user)
        response = self.client.get(self.url)
        self.assertContains(response, "Complete all 9 to earn +1000 bonus pages.")
        award = PersonalTBRCompletionAward.objects.create(
            personal_tbr=self.tbr, month=self.month, participant=self.reader,
            completion_set_fingerprint="a" * 64, configured_book_count=9,
            bonus_amount_snapshot=1000, qualified_at=timezone.now(), last_qualified_at=timezone.now(),
            effective_date=date(2026, 8, 20), is_qualifying=True,
        )
        response = self.client.get(self.url)
        self.assertContains(response, "Full Personal TBR completed · +1000 bonus pages")
        self.assertNotContains(response, "Complete all 9 to earn")
        self.assertEqual(award.bonus_amount_snapshot, 1000)

    def test_zero_incentives_and_private_registration_or_credential_data_do_not_render(self):
        secret_token = "private-reader-hardcover-token"
        private_answer = "private registration answer"
        ReaderHardcoverConnection.objects.create(
            user=self.reader_user, encrypted_token=secret_token, token_hint="oken", is_valid=True,
        )
        question = ChallengeSignupQuestion.objects.create(
            month=self.month, wording="Private question", question_type=ChallengeSignupQuestion.QuestionType.SHORT_TEXT,
            position=1,
        )
        ChallengeSignupAnswer.objects.create(enrollment=self.enrollment, question=question, value=private_answer)
        self.month.tbr_book_bonus_pages = 0; self.month.tbr_completion_bonus_pages = 0
        self.month.save(update_fields=["tbr_book_bonus_pages", "tbr_completion_bonus_pages"])
        self.client.force_login(self.reader_user)
        response = self.client.get(self.url)
        self.assertNotContains(response, "+0")
        self.assertNotContains(response, secret_token)
        self.assertNotContains(response, private_answer)
        self.assertNotContains(response, "Private question")
        self.assertNotContains(response, "Hardcover connection")
