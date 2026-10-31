from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .models import (
    CatalogBook,
    CatalogEdition,
    ChallengeMonth,
    ChallengeStaffAssignment,
    Membership,
    ModifierProvenance,
    MonthEnrollment,
    PersonalTBR,
    PersonalTBRBook,
    ReadingGroup,
    normalize_book_identity,
)
from .participation import activate_participation, deactivate_participation
from .personal_tbr import confirm_personal_tbr, replace_draft_personal_tbr


class PersonalTbrSchemaTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.host_user = User.objects.create_user("tbr-host")
        self.reader_user = User.objects.create_user("tbr-reader")
        self.other_user = User.objects.create_user("tbr-other")
        self.root_user = User.objects.create_superuser("tbr-root", "root@example.com", "password")
        self.group = ReadingGroup.objects.create(name="TBR Group", slug="tbr-group")
        self.host = Membership.objects.create(group=self.group, user=self.host_user, display_name="Host")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Reader")
        self.other = Membership.objects.create(group=self.group, user=self.other_user, display_name="Other")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="September TBR",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            status=ChallengeMonth.Status.DRAFT,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST
        )
        self.enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        self.settings_url = reverse(
            "challenge-signup-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}
        )

    def book(self, position, title=None, author="Author", **extra):
        return {
            "position": position,
            "title_snapshot": title or f"Book {position}",
            "author_snapshot": author,
            **extra,
        }

    def test_challenge_tbr_defaults_and_negative_values(self):
        self.assertFalse(self.month.tbr_enabled)
        self.assertEqual(self.month.tbr_book_bonus_pages, 0)
        self.assertEqual(self.month.tbr_completion_bonus_pages, 0)
        self.month.tbr_book_bonus_pages = -1
        self.month.tbr_completion_bonus_pages = -2
        with self.assertRaises(ValidationError):
            self.month.full_clean()

    def test_host_can_save_and_disable_tbr_without_erasing_bonuses(self):
        self.client.force_login(self.host_user)
        response = self.client.post(self.settings_url, {
            "action": "save_tbr_settings",
            "tbr-tbr_enabled": "on",
            "tbr-tbr_book_bonus_pages": "25",
            "tbr-tbr_completion_bonus_pages": "200",
        })
        self.assertRedirects(response, self.settings_url)
        self.month.refresh_from_db()
        self.assertTrue(self.month.tbr_enabled)
        self.assertEqual((self.month.tbr_book_bonus_pages, self.month.tbr_completion_bonus_pages), (25, 200))
        self.client.post(self.settings_url, {
            "action": "save_tbr_settings",
            "tbr-tbr_book_bonus_pages": "25",
            "tbr-tbr_completion_bonus_pages": "200",
        })
        self.month.refresh_from_db()
        self.assertFalse(self.month.tbr_enabled)
        self.assertEqual((self.month.tbr_book_bonus_pages, self.month.tbr_completion_bonus_pages), (25, 200))

    def test_reader_cannot_change_tbr_settings(self):
        self.client.force_login(self.reader_user)
        response = self.client.post(self.settings_url, {
            "action": "save_tbr_settings",
            "tbr-tbr_enabled": "on",
            "tbr-tbr_book_bonus_pages": "25",
            "tbr-tbr_completion_bonus_pages": "200",
        })
        self.assertEqual(response.status_code, 403)

    def test_settings_render_without_adding_registration_book_entry_ui(self):
        self.client.force_login(self.host_user)
        response = self.client.get(self.settings_url)
        self.assertContains(response, "Enable Personal TBR")
        self.assertContains(response, "exactly 9 books")
        self.assertNotContains(response, "Add TBR Book")
        self.assertNotContains(response, "Hardcover Search")

    def test_one_root_per_enrollment_but_other_enrollments_are_independent(self):
        PersonalTBR.objects.create(enrollment=self.enrollment)
        with self.assertRaises(ValidationError):
            PersonalTBR.objects.create(enrollment=self.enrollment)
        other_enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.other)
        PersonalTBR.objects.create(enrollment=other_enrollment)
        self.assertEqual(PersonalTBR.objects.count(), 2)

    def test_same_reader_may_have_a_different_tbr_in_another_challenge(self):
        other_month = ChallengeMonth.objects.create(group=self.group, name="October TBR")
        other_enrollment = MonthEnrollment.objects.create(month=other_month, participant=self.reader)
        first = replace_draft_personal_tbr(enrollment=self.enrollment, books=[self.book(1, "Shared")])
        second = replace_draft_personal_tbr(enrollment=other_enrollment, books=[self.book(1, "Shared")])
        self.assertNotEqual(first.pk, second.pk)

    def test_authority_alone_does_not_create_tbr_roots(self):
        self.client.force_login(self.host_user)
        self.client.get(self.settings_url)
        self.client.force_login(self.root_user)
        self.client.get(self.settings_url)
        self.assertFalse(PersonalTBR.objects.exists())

    def test_zero_one_and_nine_books_are_valid_and_tenth_is_rejected(self):
        tbr = replace_draft_personal_tbr(enrollment=self.enrollment, books=[])
        self.assertEqual(tbr.books.count(), 0)
        tbr = replace_draft_personal_tbr(enrollment=self.enrollment, books=[self.book(1)])
        self.assertEqual(tbr.books.count(), 1)
        tbr = replace_draft_personal_tbr(enrollment=self.enrollment, books=[self.book(i) for i in range(1, 10)])
        self.assertEqual(tbr.books.count(), 9)
        with self.assertRaises(ValidationError):
            replace_draft_personal_tbr(enrollment=self.enrollment, books=[self.book(i) for i in range(1, 11)])
        self.assertEqual(tbr.books.count(), 9)

    def test_positions_are_bounded_unique_and_ordered(self):
        with self.assertRaises(ValidationError):
            replace_draft_personal_tbr(enrollment=self.enrollment, books=[self.book(0)])
        with self.assertRaises(ValidationError):
            replace_draft_personal_tbr(enrollment=self.enrollment, books=[self.book(1), self.book(1, "Other")])
        tbr = replace_draft_personal_tbr(enrollment=self.enrollment, books=[self.book(3), self.book(1), self.book(2)])
        self.assertEqual(list(tbr.books.values_list("position", flat=True)), [1, 2, 3])

    def test_manual_and_catalog_snapshots_are_valid_and_durable(self):
        catalog = CatalogBook.objects.create(provider="hardcover", provider_book_id="tbr-work", title="Catalog", author="Writer")
        edition = CatalogEdition.objects.create(book=catalog, provider="hardcover", provider_edition_id="tbr-edition", page_count=321)
        tbr = replace_draft_personal_tbr(enrollment=self.enrollment, books=[
            self.book(1, "Manual", page_count_snapshot=None),
            self.book(2, "Catalog", author="Writer", catalog_book=catalog, catalog_edition=edition, page_count_snapshot=321),
        ])
        catalog.title = "Provider Changed"
        catalog.save()
        snapshots = list(tbr.books.values_list("title_snapshot", "page_count_snapshot"))
        self.assertEqual(snapshots, [("Manual", None), ("Catalog", 321)])

    def test_duplicate_catalog_manual_and_cross_identity_are_rejected(self):
        catalog = CatalogBook.objects.create(provider="hardcover", provider_book_id="duplicate", title="Same Book", author="Same Author")
        with self.assertRaises(ValidationError):
            replace_draft_personal_tbr(enrollment=self.enrollment, books=[
                self.book(1, "Same Book", author="Same Author", catalog_book=catalog),
                self.book(2, "Same Book", author="Same Author", catalog_book=catalog),
            ])
        with self.assertRaises(ValidationError):
            replace_draft_personal_tbr(enrollment=self.enrollment, books=[
                self.book(1, "Same—Book!", author="Same Author"),
                self.book(2, "same book", author="same author"),
            ])
        with self.assertRaises(ValidationError):
            replace_draft_personal_tbr(enrollment=self.enrollment, books=[
                self.book(1, "Same Book", author="Same Author", catalog_book=catalog),
                self.book(2, "same book", author="same author"),
            ])

    def test_title_only_similarity_is_allowed(self):
        tbr = replace_draft_personal_tbr(enrollment=self.enrollment, books=[
            self.book(1, "A Shared Title", author="First Author"),
            self.book(2, "A Shared Title", author="Second Author"),
        ])
        self.assertEqual(tbr.books.count(), 2)

    def test_existing_normalization_helper_populates_identity(self):
        tbr = replace_draft_personal_tbr(enrollment=self.enrollment, books=[self.book(1, " Héllo—World! ", author=" A. Writer ")])
        book = tbr.books.get()
        self.assertEqual(book.normalized_title, normalize_book_identity(" Héllo—World! "))
        self.assertEqual(book.normalized_author, normalize_book_identity(" A. Writer "))

    def test_confirmed_empty_fewer_than_nine_and_exactly_nine_are_valid(self):
        empty, created = confirm_personal_tbr(enrollment=self.enrollment)
        self.assertTrue(created)
        self.assertIsNotNone(empty.confirmed_at)
        other_enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.other)
        replace_draft_personal_tbr(enrollment=other_enrollment, books=[self.book(1)])
        fewer, _ = confirm_personal_tbr(enrollment=other_enrollment)
        self.assertEqual(fewer.books.count(), 1)
        third_user = get_user_model().objects.create_user("tbr-third")
        third_member = Membership.objects.create(group=self.group, user=third_user, display_name="Third")
        third_enrollment = MonthEnrollment.objects.create(month=self.month, participant=third_member)
        replace_draft_personal_tbr(enrollment=third_enrollment, books=[self.book(i) for i in range(1, 10)])
        full, _ = confirm_personal_tbr(enrollment=third_enrollment)
        self.assertEqual(full.books.count(), 9)

    def test_confirmed_tbr_cannot_add_remove_reorder_replace_or_unlock(self):
        tbr = replace_draft_personal_tbr(enrollment=self.enrollment, books=[self.book(1), self.book(2)])
        tbr, _ = confirm_personal_tbr(enrollment=self.enrollment)
        with self.assertRaises(ValidationError):
            replace_draft_personal_tbr(enrollment=self.enrollment, books=[self.book(1, "Replacement")])
        first = tbr.books.get(position=1)
        first.position = 3
        with self.assertRaises(ValidationError):
            first.save()
        with self.assertRaises(ValidationError):
            first.delete()
        with self.assertRaises(ValidationError):
            PersonalTBRBook.objects.create(personal_tbr=tbr, **self.book(3))
        tbr.confirmed_at = None
        with self.assertRaises(ValidationError):
            tbr.save()
        with self.assertRaises(ValidationError):
            tbr.delete()

    def test_withdrawal_and_reactivation_preserve_same_locked_tbr(self):
        tbr = replace_draft_personal_tbr(enrollment=self.enrollment, books=[self.book(1)])
        tbr, _ = confirm_personal_tbr(enrollment=self.enrollment)
        confirmed_at = tbr.confirmed_at
        deactivate_participation(
            enrollment=self.enrollment,
            actor=self.reader_user,
            reason=MonthEnrollment.InactiveReason.WITHDRAWN,
        )
        tbr.refresh_from_db()
        self.assertEqual(tbr.confirmed_at, confirmed_at)
        enrollment, created, reactivated = activate_participation(
            month=self.month,
            participant=self.reader,
            actor=self.reader_user,
            origin=MonthEnrollment.Origin.SELF,
        )
        self.assertFalse(created)
        self.assertTrue(reactivated)
        self.assertEqual(enrollment.pk, self.enrollment.pk)
        self.assertEqual(PersonalTBR.objects.get(enrollment=enrollment).pk, tbr.pk)

    def test_tbr_models_store_no_credentials_and_create_no_scoring(self):
        forbidden = {"token", "encrypted_token", "credential"}
        fields = {field.name for model in (PersonalTBR, PersonalTBRBook) for field in model._meta.fields}
        self.assertTrue(forbidden.isdisjoint(fields))
        replace_draft_personal_tbr(enrollment=self.enrollment, books=[self.book(1)])
        confirm_personal_tbr(enrollment=self.enrollment)
        self.assertFalse(ModifierProvenance.objects.filter(source_type__in=["tbr_book", "tbr_completion"]).exists())
