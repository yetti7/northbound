from datetime import date

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from .models import BotmBook, CatalogBook, CatalogEdition, ChallengeMonth, ReadingGroup


class BotmSchemaTests(TestCase):
    def setUp(self):
        self.group = ReadingGroup.objects.create(name="BOTM Group", slug="botm-group")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="BOTM Challenge",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
        )
        self.catalog_book = CatalogBook.objects.create(
            provider="hardcover",
            provider_book_id="book-1",
            title="The North Wind",
            author="A. Reader",
            cover_url="https://example.com/cover.jpg",
            source_url="https://example.com/book-1",
        )
        self.catalog_edition = CatalogEdition.objects.create(
            book=self.catalog_book,
            provider="hardcover",
            provider_edition_id="edition-1",
            page_count=320,
            source_url="https://example.com/edition-1",
        )

    def book(self, **overrides):
        values = {
            "month": self.month,
            "position": 1,
            "title_snapshot": "The North Wind",
            "author_snapshot": "A. Reader",
            "page_count_snapshot": 320,
        }
        values.update(overrides)
        return BotmBook(**values)

    def test_botm_defaults_disabled_with_zero_completion_bonus(self):
        self.assertFalse(self.month.botm_enabled)
        self.assertEqual(self.month.botm_completion_bonus_pages, 0)

        self.month.botm_enabled = True
        self.month.botm_completion_bonus_pages = 100
        self.month.full_clean()
        self.month.save(update_fields=["botm_enabled", "botm_completion_bonus_pages"])
        self.month.refresh_from_db()
        self.assertTrue(self.month.botm_enabled)
        self.assertEqual(self.month.botm_completion_bonus_pages, 100)

        self.month.botm_completion_bonus_pages = -1
        with self.assertRaises(ValidationError):
            self.month.full_clean()

    def test_valid_catalog_backed_book_persists_durable_snapshots(self):
        book = self.book(
            catalog_book=self.catalog_book,
            catalog_edition=self.catalog_edition,
            cover_url_snapshot=self.catalog_book.cover_url,
            source_url_snapshot=self.catalog_edition.source_url,
            bonus_pages=25,
        )
        book.save()
        self.catalog_book.title = "Updated catalog title"
        self.catalog_book.save(update_fields=["title"])
        book.refresh_from_db()
        self.assertEqual(book.title_snapshot, "The North Wind")
        self.assertEqual(book.page_count_snapshot, 320)
        self.assertEqual(book.bonus_pages, 25)

    def test_valid_manual_book_and_normalization(self):
        book = self.book(
            title_snapshot="  THE—NORTH Wind: A Tale  ",
            author_snapshot="Ａ.  READER ",
        )
        book.save()
        self.assertEqual(book.normalized_title, "the north wind a tale")
        self.assertEqual(book.normalized_author, "a reader")
        self.assertIn("a tale", book.normalized_title)
        self.assertNotEqual(book.normalized_author, "alex reader")

    def test_position_and_numeric_values_are_validated(self):
        for overrides, field in (
            ({"position": 0}, "position"),
            ({"position": 10}, "position"),
            ({"page_count_snapshot": 0}, "page_count_snapshot"),
            ({"bonus_pages": -1}, "bonus_pages"),
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError) as raised:
                self.book(**overrides).full_clean()
            self.assertIn(field, raised.exception.message_dict)

    def test_active_position_is_unique_but_retired_position_can_be_reused(self):
        first = self.book(title_snapshot="First", author_snapshot="Writer One")
        first.save()
        with self.assertRaises(ValidationError):
            self.book(title_snapshot="Second", author_snapshot="Writer Two").save()
        first.is_retired = True
        first.save(update_fields=["is_retired"])
        replacement = self.book(title_snapshot="Second", author_snapshot="Writer Two")
        replacement.save()
        self.assertEqual(self.month.botm_books.count(), 2)

    def test_same_active_catalog_book_is_rejected(self):
        self.book(catalog_book=self.catalog_book, catalog_edition=self.catalog_edition).save()
        duplicate = self.book(
            position=2,
            catalog_book=self.catalog_book,
            catalog_edition=self.catalog_edition,
            title_snapshot="Different snapshot",
        )
        with self.assertRaises(ValidationError):
            duplicate.save()

    def test_manual_duplicate_requires_normalized_title_and_author(self):
        self.book(title_snapshot="North-Wind", author_snapshot="A. Reader").save()
        with self.assertRaises(ValidationError):
            self.book(position=2, title_snapshot=" north wind ", author_snapshot="A Reader").save()
        self.book(position=2, title_snapshot="North Wind", author_snapshot="Another Reader").save()

    def test_nine_active_book_cap_and_retirement_reuse(self):
        for position in range(1, 10):
            self.book(position=position, title_snapshot=f"Book {position}", author_snapshot=f"Author {position}").save()
        with self.assertRaises(ValidationError) as raised:
            self.book(position=1, title_snapshot="Tenth", author_snapshot="Author Ten").save()
        self.assertIn("month", raised.exception.message_dict)
        first = BotmBook.objects.get(month=self.month, position=1, is_retired=False)
        first.is_retired = True
        first.save(update_fields=["is_retired"])
        self.book(position=1, title_snapshot="Tenth", author_snapshot="Author Ten").save()
        self.assertEqual(self.month.botm_books.filter(is_retired=False).count(), 9)

    def test_retired_manual_identity_can_be_reused_without_losing_snapshots(self):
        historical = self.book(title_snapshot="Old Title", author_snapshot="Old Author", bonus_pages=0)
        historical.save()
        historical.is_retired = True
        historical.save(update_fields=["is_retired"])
        replacement = self.book(title_snapshot="old title", author_snapshot="OLD AUTHOR", bonus_pages=10)
        replacement.save()
        historical.refresh_from_db()
        self.assertTrue(historical.is_retired)
        self.assertEqual((historical.title_snapshot, historical.author_snapshot), ("Old Title", "Old Author"))

    def test_catalog_edition_must_belong_to_selected_catalog_book(self):
        other = CatalogBook.objects.create(
            provider="hardcover", provider_book_id="book-2", title="Other", author="Writer"
        )
        with self.assertRaises(ValidationError):
            self.book(catalog_book=other, catalog_edition=self.catalog_edition).full_clean()

    def test_catalog_links_are_protected_while_snapshots_remain_configured(self):
        self.book(catalog_book=self.catalog_book, catalog_edition=self.catalog_edition).save()
        for linked_record in (self.catalog_edition, self.catalog_book):
            with self.subTest(model=type(linked_record).__name__), self.assertRaises(ProtectedError):
                linked_record.delete()

    def test_database_constraints_reject_direct_active_position_collision(self):
        self.book(title_snapshot="First", author_snapshot="Writer One").save()
        duplicate = self.book(title_snapshot="Second", author_snapshot="Writer Two")
        duplicate.normalized_title = "second"
        duplicate.normalized_author = "writer two"
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BotmBook.objects.bulk_create([duplicate])
