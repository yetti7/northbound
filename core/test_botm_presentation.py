from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from .botm_completion import synchronize_botm_completion_for_reader
from .models import (
    BookSubmission,
    BotmBook,
    BotmMatch,
    CatalogBook,
    CatalogEdition,
    ChallengeMonth,
    ChallengeStaffAssignment,
    Membership,
    MonthEnrollment,
    ReadingGroup,
)


class BotmReaderPresentationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.reader_user = User.objects.create_user("presentation-reader")
        self.other_user = User.objects.create_user("presentation-other")
        self.host_user = User.objects.create_user("presentation-host")
        self.root = User.objects.create_superuser("presentation-root", "root@example.com", "password")
        self.group = ReadingGroup.objects.create(name="Presentation", slug="presentation")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Reader")
        self.other = Membership.objects.create(group=self.group, user=self.other_user, display_name="Other")
        self.host = Membership.objects.create(group=self.group, user=self.host_user, display_name="Host")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="BOTM Presentation",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
            botm_enabled=True,
            botm_completion_bonus_pages=200,
        )
        MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        MonthEnrollment.objects.create(month=self.month, participant=self.other)
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.host,
            role=ChallengeStaffAssignment.Role.HOST,
        )
        self.books = [
            BotmBook.objects.create(
                month=self.month,
                position=position,
                title_snapshot=f"Book {position}",
                author_snapshot=f"Author {position}",
                page_count_snapshot=100 + position,
                bonus_pages=50 if position == 1 else 0,
                cover_url_snapshot=f"https://example.com/{position}.jpg",
            )
            for position in range(1, 4)
        ]
        self.catalog_book = CatalogBook.objects.create(
            provider="hardcover", provider_book_id="botm-work", title="Book 1", author="Author 1",
            source_url="https://hardcover.app/books/book-one",
        )
        self.catalog_edition = CatalogEdition.objects.create(
            book=self.catalog_book, provider="hardcover", provider_edition_id="botm-edition",
            source_url="https://hardcover.app/books/book-one/editions/botm-edition",
        )
        self.books[0].catalog_book = self.catalog_book
        self.books[0].catalog_edition = self.catalog_edition
        self.books[0].save(update_fields=["catalog_book", "catalog_edition"])
        self.url = reverse("botm-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})

    def submission(self, participant, book, *, status=BookSubmission.Status.APPROVED):
        return BookSubmission.objects.create(
            month=self.month,
            participant=participant,
            title=book.title_snapshot,
            author=book.author_snapshot,
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 8, 10),
            submitted_pages=book.page_count_snapshot,
            approved_pages=book.page_count_snapshot if status == BookSubmission.Status.APPROVED else None,
            status=status,
        )

    def match(self, participant, book, *, status, qualifying=False):
        submission = self.submission(participant, book)
        return BotmMatch.objects.create(
            botm_book=book,
            month=self.month,
            participant=participant,
            submission=submission,
            method=BotmMatch.Method.NORMALIZED_TITLE_AUTHOR,
            status=status,
            is_qualifying=qualifying,
            botm_title_snapshot=book.title_snapshot,
            botm_author_snapshot=book.author_snapshot,
            submission_title_snapshot=submission.title,
            submission_author_snapshot=submission.author,
        )

    def test_completed_pending_and_unfinished_states_are_distinct_and_accessible(self):
        self.match(self.reader, self.books[0], status=BotmMatch.Status.CONFIRMED, qualifying=True)
        self.match(self.reader, self.books[1], status=BotmMatch.Status.PENDING_REVIEW)
        self.client.force_login(self.reader_user)
        response = self.client.get(self.url)
        self.assertContains(response, "1 / 3 completed")
        self.assertContains(response, "Complete all 3 to earn +200 bonus pages.")
        self.assertEqual(response.content.decode().count("book-completion-overlay"), 1)
        self.assertContains(response, ">Completed<")
        self.assertContains(response, ">Pending<")
        self.assertContains(response, ">Not completed<")
        self.assertContains(response, "+50 bonus pages")
        self.assertContains(response, "page-heading page-header feature-page-header")

    def test_only_catalog_backed_book_exposes_hardcover_destination(self):
        self.client.force_login(self.reader_user)
        response = self.client.get(self.url)
        self.assertContains(response, self.catalog_edition.hardcover_url)
        self.assertContains(response, "View on Hardcover")
        self.assertContains(response, "aria-label=\"View Book 1 on Hardcover (opens in a new tab)\"")
        self.assertEqual(response.content.decode().count("View on Hardcover"), 1)
        self.assertNotContains(response, "https://hardcover.app/books/book-2")

    def test_pending_submission_is_pending_but_rejected_match_is_not(self):
        self.submission(self.reader, self.books[0], status=BookSubmission.Status.PENDING)
        self.match(self.reader, self.books[1], status=BotmMatch.Status.REJECTED)
        self.client.force_login(self.reader_user)
        response = self.client.get(self.url)
        books = response.context["active_books"]
        self.assertEqual([book.reader_state for book in books], ["pending", "not_completed", "not_completed"])
        self.assertNotContains(response, "book-completion-overlay")
        self.assertNotContains(response, "evidence_summary")

    def test_progress_excludes_retired_and_other_reader_state_is_private(self):
        self.books[2].is_retired = True
        self.books[2].save(update_fields=["is_retired"])
        self.match(self.other, self.books[0], status=BotmMatch.Status.CONFIRMED, qualifying=True)
        private_title = "Other Reader Private Submission"
        other_submission = self.submission(self.other, self.books[1])
        other_submission.title = private_title
        other_submission.save(update_fields=["title"])
        self.client.force_login(self.reader_user)
        response = self.client.get(self.url)
        self.assertContains(response, "0 / 2 completed")
        self.assertNotContains(response, "book-completion-overlay")
        self.assertNotContains(response, private_title)

    def test_qualifying_durable_award_controls_full_completion_wording(self):
        for book in self.books:
            self.match(self.reader, book, status=BotmMatch.Status.CONFIRMED, qualifying=True)
        synchronize_botm_completion_for_reader(month=self.month, participant=self.reader)
        self.client.force_login(self.reader_user)
        response = self.client.get(self.url)
        self.assertContains(response, "3 / 3 completed")
        self.assertContains(response, "Full BOTM completed · +200 bonus pages")
        self.month.botm_completion_awards.update(is_qualifying=False)
        response = self.client.get(self.url)
        self.assertNotContains(response, "Full BOTM completed")

    def test_authority_alone_has_no_reader_progress_and_preview_reuses_overlay(self):
        self.match(self.reader, self.books[0], status=BotmMatch.Status.CONFIRMED, qualifying=True)
        for user in (self.host_user, self.root):
            self.client.force_login(user)
            response = self.client.get(self.url)
            self.assertNotContains(response, " / 3 completed")
            self.assertNotContains(response, "book-completion-overlay")
        self.client.force_login(self.reader_user)
        detail = self.client.get(self.month.get_absolute_url())
        self.assertContains(detail, "1 / 3 completed")
        self.assertContains(detail, "book-completion-overlay")
        self.assertContains(detail, "images/completed-stamp.png")
        self.assertIsNotNone(finders.find("images/completed-stamp.png"))

    def test_participating_host_receives_only_their_own_reader_state(self):
        MonthEnrollment.objects.create(month=self.month, participant=self.host)
        self.match(self.host, self.books[0], status=BotmMatch.Status.CONFIRMED, qualifying=True)
        self.match(self.reader, self.books[1], status=BotmMatch.Status.CONFIRMED, qualifying=True)
        self.client.force_login(self.host_user)
        response = self.client.get(self.url)
        self.assertContains(response, "1 / 3 completed")
        self.assertEqual(response.content.decode().count("book-completion-overlay"), 1)

    def test_one_of_one_and_nine_of_nine_progress_and_zero_bonus_copy(self):
        for count in (1, 9):
            with self.subTest(count=count):
                month = ChallengeMonth.objects.create(
                    group=self.group,
                    name=f"Count {count}",
                    starts_on=date(2026, 9, 1),
                    ends_on=date(2026, 9, 30),
                    status=ChallengeMonth.Status.ACTIVE,
                    botm_enabled=True,
                    botm_completion_bonus_pages=0,
                )
                MonthEnrollment.objects.create(month=month, participant=self.reader)
                for position in range(1, count + 1):
                    book = BotmBook.objects.create(
                        month=month,
                        position=position,
                        title_snapshot=f"Count {count} Book {position}",
                        author_snapshot=f"Count Author {position}",
                        page_count_snapshot=100,
                    )
                    submission = BookSubmission.objects.create(
                        month=month,
                        participant=self.reader,
                        title=book.title_snapshot,
                        author=book.author_snapshot,
                        book_format=BookSubmission.Format.EBOOK,
                        completed_on=date(2026, 9, 10),
                        submitted_pages=100,
                        approved_pages=100,
                        status=BookSubmission.Status.APPROVED,
                    )
                    BotmMatch.objects.create(
                        botm_book=book,
                        month=month,
                        participant=self.reader,
                        submission=submission,
                        method=BotmMatch.Method.NORMALIZED_TITLE_AUTHOR,
                        status=BotmMatch.Status.CONFIRMED,
                        is_qualifying=True,
                        botm_title_snapshot=book.title_snapshot,
                        botm_author_snapshot=book.author_snapshot,
                        submission_title_snapshot=submission.title,
                        submission_author_snapshot=submission.author,
                    )
                synchronize_botm_completion_for_reader(month=month, participant=self.reader)
                self.client.force_login(self.reader_user)
                response = self.client.get(reverse("botm-list", kwargs={"group_slug": self.group.slug, "month_pk": month.pk}))
                self.assertContains(response, f"{count} / {count} completed")
                self.assertNotContains(response, "+0 bonus pages")
