from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .botm_configuration import (
    add_botm_book,
    delete_unused_botm_book,
    reactivate_botm_book,
    retire_botm_book,
    update_botm_book,
)
from .models import (
    AuditEvent,
    BookSubmission,
    BotmBook,
    BotmMatch,
    CatalogBook,
    CatalogEdition,
    ChallengeMonth,
    ChallengeStaffAssignment,
    Game,
    HardcoverConnection,
    Membership,
    ModifierProvenance,
    MonthEnrollment,
    ReadingGroup,
    Team,
    TeamAssignment,
)


class BotmManagementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.root = User.objects.create_superuser("botm-root", "root@example.com", "password")
        self.host_user = User.objects.create_user("botm-host")
        self.owner_user = User.objects.create_user("botm-owner")
        self.moderator_user = User.objects.create_user("botm-moderator")
        self.reader_user = User.objects.create_user("botm-reader")
        self.floater_user = User.objects.create_user("botm-floater")
        self.leader_user = User.objects.create_user("botm-leader")
        self.ended_user = User.objects.create_user("botm-ended")
        self.group = ReadingGroup.objects.create(name="BOTM Management", slug="botm-management")
        self.host = self.member(self.host_user, "Host")
        self.owner = self.member(self.owner_user, "Owner", Membership.Role.OWNER)
        self.moderator = self.member(self.moderator_user, "Moderator", Membership.Role.MODERATOR)
        self.reader = self.member(self.reader_user, "Reader")
        self.floater = self.member(self.floater_user, "Floater")
        self.leader = self.member(self.leader_user, "Leader")
        self.ended = self.member(self.ended_user, "Ended")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="BOTM Challenge", starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30), status=ChallengeMonth.Status.ACTIVE, botm_enabled=True,
        )
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER)
        MonthEnrollment.objects.create(month=self.month, participant=self.leader)
        team = Team.objects.create(month=self.month, name="Team")
        TeamAssignment.objects.create(month=self.month, participant=self.leader, team=team)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.leader, role=ChallengeStaffAssignment.Role.TEAM_LEADER, team=team)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.ended, role=ChallengeStaffAssignment.Role.HOST, ended_at=timezone.now())
        MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        self.list_url = reverse("botm-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.settings_url = reverse("challenge-botm-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.create_url = reverse("botm-book-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})

    def member(self, user, name, role=Membership.Role.MEMBER):
        return Membership.objects.create(group=self.group, user=user, display_name=name, role=role)

    def values(self, position=1, title="Manual Book", author="A. Writer", **overrides):
        values = {
            "position": position, "title_snapshot": title, "author_snapshot": author,
            "page_count_snapshot": 250, "cover_url_snapshot": "", "source_url_snapshot": "",
            "bonus_pages": 0,
        }
        values.update(overrides)
        return values

    def add(self, **kwargs):
        return add_botm_book(month=self.month, actor=self.host_user, values=self.values(**kwargs))

    def test_service_creates_one_and_nine_but_rejects_tenth_and_active_duplicates(self):
        with patch("core.botm_configuration.ChallengeMonth.objects.select_for_update", wraps=ChallengeMonth.objects.select_for_update) as parent_lock:
            first = self.add()
        self.assertGreaterEqual(parent_lock.call_count, 1)
        self.assertEqual(first.month, self.month)
        for position in range(2, 10):
            self.add(position=position, title=f"Book {position}", author=f"Author {position}")
        with self.assertRaises(ValidationError):
            self.add(position=1, title="Tenth", author="Author Ten")
        with self.assertRaises(ValidationError):
            self.add(position=2, title="Other", author="Other")

    def test_service_duplicate_catalog_manual_cross_identity_and_retired_limit(self):
        catalog = CatalogBook.objects.create(provider="hardcover", provider_book_id="work", title="Same—Book", author="A. Writer")
        edition = CatalogEdition.objects.create(book=catalog, provider="hardcover", provider_edition_id="edition", page_count=300)
        self.add(catalog_book=catalog, catalog_edition=edition, title="Same Book", author="A Writer")
        with self.assertRaises(ValidationError):
            self.add(position=2, catalog_book=catalog, catalog_edition=edition, title="Changed", author="Metadata")
        with self.assertRaises(ValidationError):
            self.add(position=2, title=" same book ", author="a writer")
        retire_botm_book(month=self.month, book=BotmBook.objects.get(position=1), actor=self.host_user)
        replacement = self.add(position=1, title="Same Book", author="A Writer")
        self.assertEqual(self.month.botm_books.filter(is_retired=False).count(), 1)
        self.assertEqual(replacement.position, 1)

    def test_service_atomic_position_swap_and_reactivation_rules(self):
        first = self.add(position=1, title="First", author="One")
        second = self.add(position=2, title="Second", author="Two")
        update_botm_book(month=self.month, book=first, actor=self.host_user, values={**self.values(position=2, title="First", author="One")})
        first.refresh_from_db(); second.refresh_from_db()
        self.assertEqual((first.position, second.position), (2, 1))
        retire_botm_book(month=self.month, book=first, actor=self.host_user)
        with self.assertRaises(ValidationError):
            reactivate_botm_book(month=self.month, book=first, actor=self.host_user, position=1)
        reactivate_botm_book(month=self.month, book=first, actor=self.host_user, position=2)
        self.assertEqual(self.month.botm_books.filter(is_retired=False).count(), 2)

    def test_service_authority_ownership_delete_and_audit(self):
        book = self.add()
        other_group = ReadingGroup.objects.create(name="Other", slug="botm-other")
        other_month = ChallengeMonth.objects.create(group=other_group, name="Other")
        with self.assertRaises(ValidationError):
            update_botm_book(month=other_month, book=book, actor=self.root, values=self.values())
        with self.assertRaises(ValidationError):
            add_botm_book(month=self.month, actor=self.owner_user, values=self.values(position=2, title="Denied"))
        catalog = CatalogBook.objects.create(provider="hardcover", provider_book_id="kept", title="Kept", author="Author")
        book.catalog_book = catalog
        book.save()
        delete_unused_botm_book(month=self.month, book=book, actor=self.host_user)
        self.assertTrue(CatalogBook.objects.filter(pk=catalog.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="botm.book_deleted", object_id=str(book.pk)).exists())

    def test_settings_authority_persistence_disable_and_lifecycle_lock(self):
        self.add()
        for user in (self.host_user, self.root):
            self.client.force_login(user)
            response = self.client.post(self.settings_url, {"botm_enabled": "", "botm_completion_bonus_pages": "100"})
            self.assertRedirects(response, reverse("challenge-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
            self.month.refresh_from_db()
            self.assertFalse(self.month.botm_enabled)
            self.assertEqual(self.month.botm_completion_bonus_pages, 100)
            self.assertEqual(self.month.botm_books.count(), 1)
        for user in (self.owner_user, self.moderator_user, self.reader_user, self.floater_user, self.leader_user, self.ended_user):
            self.client.force_login(user)
            self.assertEqual(self.client.post(self.settings_url, {"botm_enabled": "on", "botm_completion_bonus_pages": "10"}).status_code, 403)
        self.client.force_login(self.host_user)
        response = self.client.post(self.settings_url, {"botm_enabled": "on", "botm_completion_bonus_pages": "-1"})
        self.assertContains(response, "Ensure this value is greater than or equal to 0")
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status=ChallengeMonth.Status.FINALIZING)
        self.assertRedirects(self.client.post(self.settings_url, {"botm_enabled": "on", "botm_completion_bonus_pages": "0"}), reverse("challenge-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))

    def test_challenge_detail_and_list_reader_visibility_privacy(self):
        book = self.add(title="Visible Title", author="Visible Author", cover_url_snapshot="https://example.com/cover.jpg")
        self.month.games_enabled = True
        self.month.save(update_fields=["games_enabled"])
        now = timezone.now()
        Game.objects.create(month=self.month, name="Existing Game", starts_at=now, ends_at=now + timedelta(days=1), advertised_bonus_pages=10)
        self.client.force_login(self.reader_user)
        detail = self.client.get(self.month.get_absolute_url())
        self.assertContains(detail, f'href="{self.list_url}"><span>Book of the Month</span>')
        self.assertContains(detail, "Visible Title")
        self.assertNotContains(detail, "View Book of the Month")
        html = detail.content.decode()
        self.assertLess(html.index('class="challenge-schedule"'), html.index('class="challenge-botm-preview"'))
        self.assertLess(html.index('class="challenge-botm-preview"'), html.index('class="challenge-games-preview"'))
        self.assertIn('class="challenge-upper-layout"', html)
        page = self.client.get(self.list_url)
        self.assertContains(page, "Visible Author")
        self.assertNotContains(page, "Edit")
        self.assertNotContains(page, "Retired Books")
        retire_botm_book(month=self.month, book=book, actor=self.host_user)
        page = self.client.get(self.list_url)
        self.assertNotContains(page, "Visible Title")
        self.month.botm_enabled = False
        self.month.save(update_fields=["botm_enabled"])
        disabled = self.client.get(self.month.get_absolute_url())
        self.assertContains(disabled, "Book of the Month")
        self.assertContains(disabled, "Not enabled for this Challenge.")
        self.assertNotContains(disabled, self.list_url)
        self.assertNotContains(disabled, "Visible Title")
        self.assertEqual(self.client.get(self.list_url).status_code, 403)
        self.client.force_login(self.host_user)
        self.assertContains(self.client.get(self.list_url), "Retired Books")

    def test_enabled_empty_state_operator_controls_and_unauthorized_mutations(self):
        self.client.force_login(self.reader_user)
        self.assertContains(self.client.get(self.month.get_absolute_url()), "No Book of the Month titles have been added.")
        self.assertContains(self.client.get(self.list_url), "No Book of the Month titles have been added.")
        self.client.force_login(self.host_user)
        self.assertContains(self.client.get(self.list_url), "Add Book")
        for user in (self.owner_user, self.moderator_user, self.reader_user, self.floater_user, self.leader_user, self.ended_user):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.create_url).status_code, 403)

    def test_manual_add_edit_swap_retire_reactivate_delete_routes_and_audits(self):
        self.client.force_login(self.host_user)
        payload = {key: str(value) for key, value in self.values(title="Route Book", bonus_pages=20).items()}
        self.assertRedirects(self.client.post(self.create_url, payload), self.list_url)
        book = BotmBook.objects.get(title_snapshot="Route Book")
        second = self.add(position=2, title="Second Route", author="Other")
        edit_url = reverse("botm-book-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": book.pk})
        payload["position"] = "2"; payload["title_snapshot"] = "Edited Route"
        self.assertRedirects(self.client.post(edit_url, payload), self.list_url)
        book.refresh_from_db(); second.refresh_from_db()
        self.assertEqual((book.position, second.position), (2, 1))
        retire_url = reverse("botm-book-retire", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": book.pk})
        self.assertContains(self.client.get(retire_url), "Retire Edited Route")
        self.client.post(retire_url); book.refresh_from_db(); self.assertTrue(book.is_retired)
        reactivate_url = reverse("botm-book-reactivate", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": book.pk})
        self.assertRedirects(self.client.post(reactivate_url, {"position": "2"}), self.list_url)
        delete_url = reverse("botm-book-delete", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": book.pk})
        self.assertContains(self.client.get(delete_url), "Delete Edited Route")
        self.client.post(delete_url)
        self.assertFalse(BotmBook.objects.filter(pk=book.pk).exists())
        for action in ("botm.book_created", "botm.book_updated", "botm.book_retired", "botm.book_reactivated", "botm.book_deleted"):
            self.assertTrue(AuditEvent.objects.filter(action=action).exists(), action)

    def test_catalog_add_copies_identity_edition_and_durable_snapshots(self):
        catalog = CatalogBook.objects.create(
            provider="hardcover", provider_book_id="catalog-route", title="Catalog Title",
            author="Catalog Author", cover_url="https://example.com/catalog.jpg",
            source_url="https://example.com/work",
        )
        edition = CatalogEdition.objects.create(
            book=catalog, provider="hardcover", provider_edition_id="catalog-edition",
            page_count=333, source_url="https://example.com/edition",
        )
        token = signing.dumps(
            {"selected": edition.pk, "scoring": edition.pk, "method": "hardcover"},
            salt="northbound.catalog-selection",
        )
        self.client.force_login(self.host_user)
        payload = self.values(title="Catalog Title", author="Catalog Author", page_count_snapshot=333)
        payload["catalog_selection"] = token
        self.assertRedirects(self.client.post(self.create_url, payload), self.list_url)
        book = BotmBook.objects.get(catalog_book=catalog)
        self.assertEqual(book.catalog_edition, edition)
        self.assertEqual(
            (book.title_snapshot, book.author_snapshot, book.page_count_snapshot, book.cover_url_snapshot, book.source_url_snapshot),
            ("Catalog Title", "Catalog Author", 333, "https://example.com/catalog.jpg", "https://example.com/edition"),
        )
        catalog.title = "Externally Changed"
        catalog.save(update_fields=["title"])
        book.refresh_from_db()
        self.assertEqual(book.title_snapshot, "Catalog Title")

    def test_catalog_endpoint_selection_token_survives_real_form_post(self):
        HardcoverConnection.objects.create(group=self.group, encrypted_token="encrypted", is_valid=True)
        catalog = CatalogBook.objects.create(
            provider="hardcover", provider_book_id="endpoint-work", title="Endpoint Title",
            author="Endpoint Author", cover_url="https://example.com/endpoint.jpg",
            source_url="https://example.com/endpoint-work",
        )
        edition = CatalogEdition.objects.create(
            book=catalog, provider="hardcover", provider_edition_id="991122",
            page_count=412, source_url="https://example.com/endpoint-edition",
        )
        edition_payload = {
            "book_id": catalog.provider_book_id, "edition_id": edition.provider_edition_id,
            "title": catalog.title, "author": catalog.author, "format": "Hardcover",
            "pages": edition.page_count, "cover_url": catalog.cover_url,
            "source_url": edition.source_url,
        }
        catalog_url = reverse("botm-catalog", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.client.force_login(self.host_user)
        with (
            patch("core.views.decrypt_token", return_value="token"),
            patch("core.views.lookup_edition", return_value=(edition_payload, False)),
            patch("core.views.resolve_scoring_edition", return_value=(edition_payload, "hardcover")),
        ):
            response = self.client.post(catalog_url, {"action": "edition", "edition_id": edition.provider_edition_id})
        self.assertEqual(response.status_code, 200)
        selection = response.json()["result"]["catalog_selection"]
        payload = self.values(title=catalog.title, author=catalog.author, page_count_snapshot=412)
        payload.update({"catalog_selection": selection, "entry_mode": "catalog"})
        self.assertRedirects(self.client.post(self.create_url, payload), self.list_url)
        book = BotmBook.objects.get(title_snapshot="Endpoint Title")
        book.refresh_from_db()
        self.assertEqual((book.catalog_book_id, book.catalog_edition_id), (catalog.pk, edition.pk))
        self.assertEqual(
            (book.page_count_snapshot, book.cover_url_snapshot, book.source_url_snapshot),
            (412, catalog.cover_url, edition.source_url),
        )

    def test_current_botm_and_submission_catalog_forms_reuse_work_and_match_canonically(self):
        catalog = CatalogBook.objects.create(
            provider="hardcover", provider_book_id="446681", title="Dungeon Crawler Carl",
            author="Matt Dinniman", source_url="https://hardcover.app/books/dungeon-crawler-carl",
        )
        botm_edition = CatalogEdition.objects.create(
            book=catalog, provider="hardcover", provider_edition_id="30407789",
            format_name="Hardcover", page_count=464,
            source_url="https://hardcover.app/books/dungeon-crawler-carl/editions/30407789",
        )
        submission_edition = CatalogEdition.objects.create(
            book=catalog, provider="hardcover", provider_edition_id="31289995",
            format_name="Kindle", page_count=446,
            source_url="https://hardcover.app/books/dungeon-crawler-carl/editions/31289995",
        )
        botm_selection = signing.dumps(
            {"selected": botm_edition.pk, "scoring": botm_edition.pk, "method": BookSubmission.VerificationMethod.HARDCOVER},
            salt="northbound.catalog-selection",
        )
        self.client.force_login(self.host_user)
        botm_payload = self.values(title="Ignored", author="Ignored", page_count_snapshot=1)
        botm_payload.update({"catalog_selection": botm_selection, "entry_mode": "catalog"})
        self.assertRedirects(self.client.post(self.create_url, botm_payload), self.list_url)

        submission_selection = signing.dumps(
            {"selected": submission_edition.pk, "scoring": submission_edition.pk, "method": BookSubmission.VerificationMethod.HARDCOVER},
            salt="northbound.catalog-selection",
        )
        self.client.force_login(self.reader_user)
        submission_url = reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.assertRedirects(self.client.post(submission_url, {
            "catalog_selection": submission_selection,
            "title": "Ignored", "author": "Ignored", "book_format": BookSubmission.Format.OTHER,
            "started_on": "2026-09-01", "completed_on": "2026-09-10",
            "submitted_pages": "1", "reference_url": "", "notes": "",
        }), self.month.get_absolute_url())

        book = BotmBook.objects.get(title_snapshot="Dungeon Crawler Carl")
        submission = BookSubmission.objects.get(title="Dungeon Crawler Carl")
        match = BotmMatch.objects.get(botm_book=book, submission=submission)
        self.assertEqual((book.catalog_book_id, submission.catalog_book_id), (catalog.pk, catalog.pk))
        self.assertNotEqual(book.catalog_edition_id, submission.catalog_edition_id)
        self.assertEqual(match.method, BotmMatch.Method.CATALOG_WORK)
        self.assertEqual((match.botm_catalog_identity, match.submission_catalog_identity), ("hardcover:446681", "hardcover:446681"))

    def test_botm_add_renders_shared_catalog_js_contract_with_current_cache_version(self):
        HardcoverConnection.objects.create(group=self.group, encrypted_token="encrypted", is_valid=True)
        self.client.force_login(self.host_user)
        response = self.client.get(self.create_url)
        self.assertContains(response, 'data-catalog-tools')
        self.assertContains(response, f'data-catalog-url="{reverse("botm-catalog", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})}"')
        self.assertContains(response, 'data-botm-form')
        self.assertContains(response, 'id="id_catalog_selection"')
        self.assertContains(response, 'id="id_entry_mode"')
        self.assertContains(response, '/static/js/app.js?v=12')

    def test_manual_form_post_has_no_catalog_identity_and_missing_catalog_token_cannot_fall_back(self):
        self.client.force_login(self.host_user)
        payload = self.values(title="Definitely Manual", author="Manual Author")
        self.assertRedirects(self.client.post(self.create_url, payload), self.list_url)
        manual = BotmBook.objects.get(title_snapshot="Definitely Manual")
        self.assertIsNone(manual.catalog_book_id)
        self.assertIsNone(manual.catalog_edition_id)

        broken = self.values(position=2, title="Looks Imported", author="Catalog Author")
        broken["entry_mode"] = "catalog"
        response = self.client.post(self.create_url, broken)
        self.assertContains(response, "The Hardcover selection was not retained")
        self.assertFalse(BotmBook.objects.filter(title_snapshot="Looks Imported").exists())

    def test_configuration_creates_no_matching_provenance_or_score_effect(self):
        submission = BookSubmission.objects.create(
            month=self.month, participant=self.reader, title="Manual Book", author="A. Writer",
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 9, 5),
            submitted_pages=200, approved_pages=200, bonus_pages=0, final_scored_pages=200,
            status=BookSubmission.Status.APPROVED,
        )
        before = (submission.bonus_pages, submission.final_scored_pages, ModifierProvenance.objects.count())
        self.add()
        submission.refresh_from_db()
        self.assertEqual((submission.bonus_pages, submission.final_scored_pages, ModifierProvenance.objects.count()), before)
        match = BotmMatch.objects.get(submission=submission)
        self.assertEqual((match.status, match.is_qualifying), (BotmMatch.Status.CONFIRMED, True))
