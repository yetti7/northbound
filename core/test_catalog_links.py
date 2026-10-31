from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from .catalog_links import safe_hardcover_url
from .models import BookSubmission, CatalogBook, ChallengeMonth, Membership, ReadingGroup


class HardcoverCatalogLinkSafetyTests(TestCase):
    def test_only_authoritative_public_hardcover_book_urls_are_returned(self):
        self.assertEqual(
            safe_hardcover_url(
                provider="hardcover",
                source_url="https://www.hardcover.app/books/example/editions/123?token=secret#fragment",
            ),
            "https://hardcover.app/books/example/editions/123",
        )
        for provider, url in (
            ("local", "https://hardcover.app/books/example"),
            ("hardcover", "https://example.com/books/example"),
            ("hardcover", "https://hardcover.app/search?q=example"),
            ("hardcover", "http://hardcover.app/books/example"),
            ("hardcover", ""),
        ):
            with self.subTest(provider=provider, url=url):
                self.assertEqual(safe_hardcover_url(provider=provider, source_url=url), "")

    def test_manual_submission_does_not_gain_a_guessed_link(self):
        User = get_user_model()
        user = User.objects.create_user("manual-link-reader")
        group = ReadingGroup.objects.create(name="Manual Links", slug="manual-links")
        member = Membership.objects.create(group=group, user=user, display_name="Manual Reader")
        month = ChallengeMonth.objects.create(
            group=group, name="Manual Challenge", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.ACTIVE,
        )
        manual = BookSubmission.objects.create(
            month=month, participant=member, title="Looks Like a Hardcover Book", author="Author",
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 10),
            submitted_pages=100, reference_url="https://hardcover.app/books/guessed-title",
        )
        self.assertEqual(manual.hardcover_url, "")
        unsafe_catalog = CatalogBook.objects.create(
            provider="hardcover", provider_book_id="unsafe", title="Unsafe", author="Author",
            source_url="https://example.com/books/unsafe",
        )
        manual.catalog_book = unsafe_catalog
        self.assertEqual(manual.hardcover_url, "")
