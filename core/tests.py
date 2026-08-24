from datetime import date, datetime, timedelta, timezone as datetime_timezone
import shutil
import tempfile
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .integrations.hardcover import HardcoverLinkError, lookup_edition, parse_hardcover_url, resolve_scoring_edition, search_books
from .integrations.secrets import decrypt_token
from .models import AuditEvent, BookSubmission, CatalogBook, CatalogEdition, CatalogSearchCache, ChallengeMonth, HardcoverConnection, Membership, MonthEnrollment, MonthTheme, PlatformBackupSettings, PlatformOwnerInvitation, PlatformSettings, ReadingGroup, Team, TeamAssignment, ThemeClaim, UserProfile
from .permissions import CAPABILITIES


class ProfilePictureTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()
        self.user = get_user_model().objects.create_user(
            "avatar-reader", email="reader@example.com", password="test-password"
        )

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_user_can_upload_and_display_profile_picture(self):
        self.client.force_login(self.user)
        gif = SimpleUploadedFile(
            "avatar.gif",
            b"GIF87a\x01\x00\x01\x00\x80\x01\x00\x00\x00\x00ccc,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;",
            content_type="image/gif",
        )
        response = self.client.post(reverse("account"), {
            "username": self.user.username,
            "first_name": "Avatar",
            "last_name": "Reader",
            "email": self.user.email,
            "profile_picture": gif,
        })
        self.assertRedirects(response, reverse("account"))
        profile = UserProfile.objects.get(user=self.user)
        self.assertTrue(profile.profile_picture.name.endswith(".gif"))
        stats = self.client.get(reverse("my-stats"))
        self.assertContains(stats, profile.profile_picture.url)

    def test_user_can_choose_built_in_avatar(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("account"), {
            "username": self.user.username,
            "first_name": "Avatar",
            "last_name": "Reader",
            "email": self.user.email,
            "selected_avatar": "3d_1.png",
        })
        self.assertRedirects(response, reverse("account"))
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.selected_avatar, "3d_1.png")
        self.assertEqual(profile.avatar_url, "/static/avatars/3d_1.png")
        self.assertContains(self.client.get(reverse("my-stats")), profile.avatar_url)

    @override_settings(NORTHBOUND_MAX_PROFILE_PICTURE_BYTES=32)
    def test_profile_picture_upload_has_a_size_limit(self):
        self.client.force_login(self.user)
        gif = SimpleUploadedFile(
            "oversized.gif",
            b"GIF87a\x01\x00\x01\x00\x80\x01\x00\x00\x00\x00ccc,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;",
            content_type="image/gif",
        )

        response = self.client.post(reverse("account"), {
            "username": self.user.username,
            "first_name": "Avatar",
            "last_name": "Reader",
            "email": self.user.email,
            "profile_picture": gif,
        })

        self.assertContains(response, "Profile pictures must be")
        self.assertFalse(UserProfile.objects.get(user=self.user).profile_picture)


class PermissionOverrideTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("permission-owner", password="test-password")
        self.reader = User.objects.create_user("permission-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Permission Group", slug="permission-group")
        self.owner_membership = Membership.objects.create(group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner")
        self.reader_membership = Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Reader")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Permission Month", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.DRAFT)

    def permission_payload(self, role=Membership.Role.READER, **overrides):
        payload = {"role": role}
        payload.update({capability: overrides.get(capability, "inherit") for capability in CAPABILITIES})
        return payload

    def test_owner_can_grant_team_management_without_month_management(self):
        self.client.force_login(self.owner)
        url = reverse("participant-permissions-edit", kwargs={"group_slug": self.group.slug, "pk": self.reader_membership.pk})
        response = self.client.post(url, self.permission_payload(manage_teams="allow"))
        self.assertRedirects(response, reverse("participant-list", kwargs={"group_slug": self.group.slug}))
        self.reader_membership.refresh_from_db()
        self.assertEqual(self.reader_membership.permission_overrides, {"manage_teams": True})

        self.client.force_login(self.reader)
        team_url = reverse("team-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.assertEqual(self.client.get(team_url).status_code, 200)
        self.assertEqual(self.client.get(reverse("month-create", kwargs={"group_slug": self.group.slug})).status_code, 403)

    def test_owner_cannot_change_own_permissions(self):
        self.client.force_login(self.owner)
        url = reverse("participant-permissions-edit", kwargs={"group_slug": self.group.slug, "pk": self.owner_membership.pk})
        self.assertRedirects(self.client.get(url), reverse("participant-list", kwargs={"group_slug": self.group.slug}))


class PlatformAccountManagementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.root = User.objects.create_superuser("developer-root", "root@example.com", "root-password")
        self.reader = User.objects.create_user("managed-reader", "reader@example.com", "old-reader-password")
        self.deactivated_reader = User.objects.create_user("archived-reader", "archived@example.com", "old-reader-password", is_active=False)
        self.group = ReadingGroup.objects.create(name="Managed Group", slug="managed-group")
        self.membership = Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Managed Reader")

    def test_root_user_directory_excludes_root_and_shows_memberships(self):
        self.client.force_login(self.root)
        listing = self.client.get(reverse("config-user-list"))
        self.assertContains(listing, "managed-reader")
        self.assertNotIn(self.root, list(listing.context["users"]))
        detail = self.client.get(reverse("config-user-detail", kwargs={"pk": self.reader.pk}))
        self.assertContains(detail, "Managed Group")
        self.assertContains(detail, reverse("participant-permissions-edit", kwargs={"group_slug": self.group.slug, "pk": self.membership.pk}))

    def test_dashboard_account_summary_links_to_filtered_directory(self):
        self.client.force_login(self.root)
        response = self.client.get(reverse("config-dashboard"))
        self.assertEqual(response.context["active_account_count"], 1)
        self.assertEqual(response.context["deactivated_account_count"], 1)
        self.assertContains(response, reverse("config-user-list"))
        self.assertContains(response, "Active")
        self.assertContains(response, "Deactivated")
        self.assertNotContains(response, "Challenge Months")
        self.assertNotContains(response, "User Management")

    def test_account_directory_exposes_identity_only_live_filters_and_sorting(self):
        self.client.force_login(self.root)
        response = self.client.get(reverse("config-user-list"))
        self.assertContains(response, 'data-account-search')
        self.assertContains(response, 'value="active"')
        self.assertContains(response, 'value="deactivated"')
        self.assertContains(response, 'value="all"')
        self.assertContains(response, 'data-account-sort="account"')
        self.assertContains(response, 'data-account-sort="status"')
        self.assertContains(response, 'data-search="managed-reader  reader@example.com"')
        self.assertNotContains(response, 'data-search="managed-reader  reader@example.com active"')
        self.assertContains(response, "Deactivated")

    def test_temporary_password_forces_replacement_and_is_audited(self):
        self.client.force_login(self.root)
        reset_url = reverse("config-user-password-reset", kwargs={"pk": self.reader.pk})
        response = self.client.post(reset_url)
        temporary_password = response.context["temporary_password"]
        self.assertTrue(temporary_password)
        profile = UserProfile.objects.get(user=self.reader)
        self.assertTrue(profile.must_change_password)
        self.assertTrue(AuditEvent.objects.filter(action="account.temporary_password_issued", object_id=str(self.reader.pk)).exists())
        self.assertNotIn(temporary_password, AuditEvent.objects.get(action="account.temporary_password_issued", object_id=str(self.reader.pk)).summary)

        self.client.logout()
        self.assertTrue(self.client.login(username=self.reader.username, password=temporary_password))
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("password-change"))
        change = self.client.post(reverse("password-change"), {
            "old_password": temporary_password,
            "new_password1": "replacement-reader-password-842!",
            "new_password2": "replacement-reader-password-842!",
        })
        self.assertRedirects(change, reverse("account"))
        profile.refresh_from_db()
        self.assertFalse(profile.must_change_password)
        self.assertTrue(AuditEvent.objects.filter(action="account.temporary_password_replaced", actor=self.reader).exists())

    def test_root_can_deactivate_and_reactivate_account(self):
        self.client.force_login(self.root)
        url = reverse("config-user-status-toggle", kwargs={"pk": self.reader.pk})
        self.client.post(url)
        self.reader.refresh_from_db()
        self.assertFalse(self.reader.is_active)
        self.assertTrue(Membership.objects.filter(pk=self.membership.pk).exists())
        self.client.post(url)
        self.reader.refresh_from_db()
        self.assertTrue(self.reader.is_active)


class PlatformGroupManagementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.root = User.objects.create_superuser("group-platform-owner", "root@example.com", "root-password")
        self.owner = User.objects.create_user("central-owner", "owner@example.com", "owner-password")
        self.reader = User.objects.create_user("central-reader", "reader@example.com", "reader-password")
        self.group = ReadingGroup.objects.create(name="Central Reading Group", slug="central-reading-group")
        self.inactive_group = ReadingGroup.objects.create(name="Inactive Archive", slug="inactive-archive", is_active=False)
        self.owner_membership = Membership.objects.create(
            group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Central Owner"
        )
        self.reader_membership = Membership.objects.create(
            group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Central Reader"
        )
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="August Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.OPEN,
        )
        AuditEvent.objects.create(
            actor=self.owner,
            group=self.group,
            action="group.test_activity",
            object_type="ReadingGroup",
            object_id=str(self.group.pk),
            summary="Updated the group for directory testing.",
        )

    def test_group_directory_lists_all_groups_with_identity_only_filters_and_summaries(self):
        self.client.force_login(self.root)
        response = self.client.get(reverse("config-group-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Central Reading Group")
        self.assertContains(response, "Inactive Archive")
        self.assertContains(response, "Central Owner")
        self.assertContains(response, "August Challenge")
        self.assertContains(response, "Updated the group for directory testing.")
        self.assertContains(response, 'data-search="central reading group central-reading-group"')
        self.assertNotContains(response, 'data-search="central reading group central-reading-group active"')
        self.assertContains(response, 'value="active"')
        self.assertContains(response, 'value="inactive"')
        self.assertContains(response, 'value="all"')

        groups = {group.slug: group for group in response.context["groups"]}
        self.assertEqual(groups[self.group.slug].participant_count, 2)
        self.assertEqual(groups[self.group.slug].current_challenge, self.month)
        self.assertEqual(groups[self.group.slug].directory_owners, [self.owner_membership])

    def test_dashboard_and_overview_link_to_central_group_management_without_membership(self):
        self.client.force_login(self.root)
        dashboard = self.client.get(reverse("config-dashboard"))
        self.assertContains(dashboard, reverse("config-group-list"))
        overview = self.client.get(reverse("config-group-detail", kwargs={"group_slug": self.group.slug}))
        self.assertContains(overview, reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.assertContains(overview, "Central Owner")
        self.assertFalse(Membership.objects.filter(group=self.group, user=self.root).exists())

        inactive_overview = self.client.get(
            reverse("config-group-detail", kwargs={"group_slug": self.inactive_group.slug})
        )
        self.assertEqual(inactive_overview.status_code, 200)
        self.assertContains(inactive_overview, "Reactivate")

    def test_platform_owner_creation_uses_normal_group_rules_without_hidden_membership(self):
        self.client.force_login(self.root)
        response = self.client.post(reverse("group-create"), {
            "name": "Platform Created Group",
            "timezone": "America/New_York",
            "announcement": "",
            "hardcover_api_token": "",
        })
        group = ReadingGroup.objects.get(name="Platform Created Group")
        self.assertRedirects(response, reverse("config-group-detail", kwargs={"group_slug": group.slug}))
        self.assertEqual(group.slug, "platform-created-group")
        self.assertEqual(len(group.join_code), 6)
        self.assertFalse(Membership.objects.filter(group=group, user=self.root).exists())
        self.assertFalse(group.memberships.exists())
        self.assertTrue(AuditEvent.objects.filter(action="group.created", group=group, actor=self.root).exists())

    def test_deactivation_and_reactivation_preserve_group_url_and_history_and_are_audited(self):
        self.client.force_login(self.root)
        url = reverse("config-group-status-toggle", kwargs={"group_slug": self.group.slug})
        confirmation = self.client.get(url)
        self.assertContains(confirmation, "Deactivate Central Reading Group?")
        self.assertTrue(ReadingGroup.objects.get(pk=self.group.pk).is_active)

        response = self.client.post(url, {"reason": "Season complete"})
        self.assertRedirects(response, reverse("config-group-detail", kwargs={"group_slug": self.group.slug}))
        self.group.refresh_from_db()
        self.assertFalse(self.group.is_active)
        self.assertEqual(self.group.slug, "central-reading-group")
        self.assertTrue(Membership.objects.filter(pk=self.owner_membership.pk).exists())
        self.assertTrue(Membership.objects.filter(pk=self.reader_membership.pk).exists())
        self.assertTrue(ChallengeMonth.objects.filter(pk=self.month.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="group.deactivated", group=self.group).exists())

        self.client.post(url)
        self.group.refresh_from_db()
        self.assertTrue(self.group.is_active)
        self.assertTrue(AuditEvent.objects.filter(action="group.reactivated", group=self.group).exists())

    def test_regular_accounts_cannot_use_central_group_management(self):
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(reverse("config-group-list")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("config-group-detail", kwargs={"group_slug": self.group.slug})).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse("config-group-status-toggle", kwargs={"group_slug": self.group.slug})).status_code,
            403,
        )


class HardcoverCatalogServiceTests(TestCase):
    def test_parses_book_and_edition_links(self):
        edition = parse_hardcover_url("https://hardcover.app/books/carls-doomsday-scenario/editions/30407787")
        self.assertEqual(edition, {"slug": "carls-doomsday-scenario", "edition_id": 30407787})
        book = parse_hardcover_url("https://www.hardcover.app/books/carls-doomsday-scenario/")
        self.assertEqual(book, {"slug": "carls-doomsday-scenario", "edition_id": None})

    def test_rejects_non_hardcover_reference_link(self):
        with self.assertRaises(HardcoverLinkError):
            parse_hardcover_url("https://example.com/books/not-hardcover")

    @patch("core.integrations.hardcover.execute_graphql")
    def test_search_normalizes_and_reuses_short_lived_cache(self, execute):
        execute.return_value = {"search": {"results": {"hits": [{"document": {
            "id": "446680", "title": "Carl's Doomsday Scenario", "subtitle": "Dungeon Crawler Carl Book 2",
            "author_names": ["Matt Dinniman"], "pages": 385, "slug": "carls-doomsday-scenario",
            "image": {"url": "https://assets.hardcover.app/example.jpg"},
        }}]}}}
        first, first_cached = search_books("token", "Carl's Doomsday Scenario")
        second, second_cached = search_books("token", "  Carl's   Doomsday Scenario ")
        self.assertFalse(first_cached)
        self.assertTrue(second_cached)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["author"], "Matt Dinniman")
        self.assertEqual(first[0]["default_pages"], 385)
        self.assertEqual(CatalogSearchCache.objects.count(), 1)
        execute.assert_called_once()

    @patch("core.integrations.hardcover.execute_graphql")
    def test_edition_lookup_persists_normalized_metadata_and_reuses_it(self, execute):
        execute.return_value = {"editions_by_pk": {
            "id": 30407787, "book_id": 446680, "title": "Carl's Doomsday Scenario", "subtitle": None,
            "isbn_10": "0593820274", "isbn_13": "9780593820278", "pages": 351, "audio_seconds": None,
            "edition_format": "Paperback", "physical_format": None,
            "cached_contributors": [{"author": {"name": "Matt Dinniman"}}],
            "image": {"url": "https://assets.hardcover.app/edition.jpg"},
            "book": {"id": 446680, "title": "Carl's Doomsday Scenario", "slug": "carls-doomsday-scenario"},
        }}
        first, first_cached = lookup_edition("token", 30407787)
        second, second_cached = lookup_edition("token", 30407787)
        self.assertFalse(first_cached)
        self.assertTrue(second_cached)
        self.assertEqual(first, second)
        self.assertEqual(first["pages"], 351)
        self.assertEqual(first["format"], "Paperback")
        self.assertEqual(CatalogBook.objects.count(), 1)
        self.assertEqual(CatalogEdition.objects.count(), 1)
        execute.assert_called_once()

    @patch("core.integrations.hardcover.lookup_edition")
    @patch("core.integrations.hardcover.list_book_editions")
    def test_audio_scoring_prefers_ebook_then_print(self, list_editions, lookup):
        selected = {"book_id": "446680", "edition_id": "audio-1", "format": "Audiobook", "audio_seconds": 36000, "pages": None}
        list_editions.return_value = [
            {"edition_id": "ebook-1", "format": "E-book", "pages": 351},
            {"edition_id": "print-1", "format": "Paperback", "pages": 385},
        ]
        lookup.return_value = ({"edition_id": "ebook-1", "format": "E-book", "pages": 351}, False)
        scoring, method = resolve_scoring_edition("token", selected)
        self.assertEqual(scoring["edition_id"], "ebook-1")
        self.assertEqual(method, BookSubmission.VerificationMethod.HARDCOVER_AUDIO)
        lookup.assert_called_once_with("token", "ebook-1")


class HardcoverConnectionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("hardcover-owner", password="test-password")
        self.reader = User.objects.create_user("hardcover-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Hardcover Group", slug="hardcover-group")
        Membership.objects.create(group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner")
        Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Reader")
        self.url = reverse("group-hardcover-connection", kwargs={"group_slug": self.group.slug})
        self.edit_url = reverse("group-edit", kwargs={"group_slug": self.group.slug})

    @patch("core.views.test_catalog_connection", return_value=True)
    def test_owner_saves_encrypted_catalog_token(self, test_connection):
        self.client.force_login(self.owner)
        token = "catalog-token-secret-1234"
        response = self.client.post(self.edit_url, {"action": "save_hardcover", "api_token": token})
        self.assertRedirects(response, self.edit_url)
        connection = HardcoverConnection.objects.get(group=self.group)
        self.assertNotIn(token, connection.encrypted_token)
        self.assertEqual(decrypt_token(connection.encrypted_token), token)
        self.assertEqual(connection.token_hint, "1234")
        self.assertTrue(connection.is_valid)
        test_connection.assert_called_once_with(token)

    def test_reader_cannot_manage_group_catalog_connection(self):
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_disconnect_requires_confirmation_page(self):
        connection = HardcoverConnection.objects.create(group=self.group, encrypted_token="encrypted", token_hint="1234")
        self.client.force_login(self.owner)
        disconnect_url = reverse("group-hardcover-disconnect", kwargs={"group_slug": self.group.slug})
        confirmation = self.client.get(disconnect_url)
        self.assertContains(confirmation, "Disconnect Hardcover?")
        self.assertTrue(HardcoverConnection.objects.filter(pk=connection.pk).exists())
        response = self.client.post(disconnect_url)
        self.assertRedirects(response, self.edit_url)
        self.assertFalse(HardcoverConnection.objects.filter(pk=connection.pk).exists())

    @patch("core.views.test_catalog_connection", return_value=True)
    def test_new_group_can_include_tested_catalog_token(self, test_connection):
        self.client.force_login(self.reader)
        token = "new-group-token-5678"
        response = self.client.post(reverse("group-create"), {
            "name": "Connected New Group",
            "timezone": "America/New_York",
            "hardcover_api_token": token,
        })
        group = ReadingGroup.objects.get(name="Connected New Group")
        self.assertRedirects(response, reverse("group-detail", kwargs={"group_slug": group.slug}))
        connection = HardcoverConnection.objects.get(group=group)
        self.assertEqual(decrypt_token(connection.encrypted_token), token)
        test_connection.assert_called_once_with(token)

    @patch("core.views.test_catalog_connection", return_value=True)
    def test_authenticated_user_can_test_token_without_saving_it(self, test_connection):
        self.client.force_login(self.reader)
        response = self.client.post(reverse("hardcover-test-token"), {"api_token": "unsaved-token"})
        self.assertJSONEqual(response.content, {"ok": True, "message": "Hardcover catalog access is working."})
        self.assertFalse(HardcoverConnection.objects.filter(group=self.group).exists())
        test_connection.assert_called_once_with("unsaved-token")

    def test_group_home_does_not_add_a_hardcover_button(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.assertNotContains(response, ">Hardcover</a>")


class FirstRunSetupTests(TestCase):
    def test_fresh_install_redirects_to_setup_wizard(self):
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("setup"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("setup")).status_code, 200)

    def test_setup_creates_initial_platform_owner(self):
        response = self.client.post(reverse("setup"), {
            "username": "taylor",
            "email": "taylor@example.com",
            "password1": "a-very-long-test-password-942!",
            "password2": "a-very-long-test-password-942!",
        })
        self.assertRedirects(response, reverse("config-dashboard"))
        user = get_user_model().objects.get(username="taylor")
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
        self.assertFalse(ReadingGroup.objects.exists())
        self.assertFalse(Membership.objects.filter(user=user).exists())
        self.assertTrue(AuditEvent.objects.filter(
            actor=user,
            action="platform.initial_owner_created",
            object_id=str(user.pk),
        ).exists())

    def test_setup_is_unavailable_after_an_owner_exists(self):
        get_user_model().objects.create_superuser("existing-owner", "owner@example.com", "test-password-482!")
        response = self.client.get(reverse("setup"))
        self.assertRedirects(response, reverse("config-login"))


class MyStatsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.reader = User.objects.create_user("stats-reader", password="test-password")
        self.other = User.objects.create_user("stats-other", password="test-password")
        self.group = ReadingGroup.objects.create(name="Stats Group", slug="stats-group")
        self.membership = Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Stats Reader")
        other_membership = Membership.objects.create(group=self.group, user=self.other, role=Membership.Role.READER, display_name="Other Reader")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Stats Month", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.OPEN)
        MonthEnrollment.objects.create(month=self.month, participant=self.membership)
        team = Team.objects.create(month=self.month, name="North Team")
        TeamAssignment.objects.create(month=self.month, team=team, participant=self.membership)
        BookSubmission.objects.create(month=self.month, participant=self.membership, title="My Approved Book", author="My Author", book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 12), submitted_pages=300, approved_pages=290, status=BookSubmission.Status.APPROVED)
        BookSubmission.objects.create(month=self.month, participant=self.membership, title="My Pending Book", author="Pending Author", book_format=BookSubmission.Format.PAPERBACK, completed_on=date(2026, 8, 14), submitted_pages=200)
        BookSubmission.objects.create(month=self.month, participant=other_membership, title="Other Private Book", author="Other Author", book_format=BookSubmission.Format.HARDCOVER, completed_on=date(2026, 8, 15), submitted_pages=999, approved_pages=999, status=BookSubmission.Status.APPROVED)

    def test_my_stats_are_account_wide_and_private(self):
        self.client.force_login(self.reader)
        response = self.client.get(reverse("my-stats"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["approved_books"], 1)
        self.assertEqual(response.context["approved_pages"], 290)
        self.assertEqual(response.context["group_count"], 1)
        self.assertEqual(response.context["month_count"], 1)
        self.assertContains(response, "Stats Group")
        self.assertContains(response, "Stats Month")
        self.assertContains(response, "North Team")
        self.assertContains(response, "My Approved Book")
        self.assertContains(response, "My Pending Book")
        self.assertNotContains(response, "Other Private Book")
        self.assertNotContains(response, "999")

    def test_account_menu_links_to_my_stats(self):
        self.client.force_login(self.reader)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, f'href="{reverse("my-stats")}">My Stats</a>')


class SubmissionWorkflowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.reader = User.objects.create_user("reader", password="test-password")
        self.mod = User.objects.create_user("mod", password="test-password")
        self.group = ReadingGroup.objects.create(name="Test Group", slug="test-group")
        self.reader_membership = Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Reader")
        Membership.objects.create(group=self.group, user=self.mod, role=Membership.Role.MODERATOR, display_name="Mod")
        self.month = ChallengeMonth.objects.create(group=self.group, name="August 2026", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.OPEN)
        MonthEnrollment.objects.create(month=self.month, participant=self.reader_membership)

    def test_reader_submits_and_moderator_approves_pages(self):
        self.client.force_login(self.reader)
        response = self.client.post(reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), {
            "title": "A Test Book",
            "author": "An Author",
            "book_format": BookSubmission.Format.PAPERBACK,
            "started_on": "2026-08-02",
            "completed_on": "2026-08-12",
            "submitted_pages": 438,
            "reference_url": "https://example.com/books/a-test-book-paperback",
            "notes": "",
        })
        self.assertRedirects(response, self.month.get_absolute_url())
        submission = BookSubmission.objects.get()
        self.assertEqual(submission.status, BookSubmission.Status.PENDING)
        self.assertEqual(submission.started_on, date(2026, 8, 2))
        self.assertEqual(submission.reference_url, "https://example.com/books/a-test-book-paperback")

        self.client.force_login(self.mod)
        response = self.client.post(reverse("submission-review", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": submission.pk}), {
            "approved_pages": 416,
            "status": BookSubmission.Status.APPROVED,
            "verification_url": "https://publisher.example/books/a-test-book",
            "review_notes": "Matched paperback edition.",
        })
        self.assertRedirects(response, reverse("review-queue", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        submission.refresh_from_db()
        self.assertEqual(submission.submitted_pages, 438)
        self.assertEqual(submission.approved_pages, 416)
        self.assertEqual(submission.reviewed_by, self.mod)
        self.assertEqual(submission.verification_url, "https://publisher.example/books/a-test-book")

    def test_review_page_links_to_reader_reference_in_new_tab(self):
        submission = BookSubmission.objects.create(
            month=self.month,
            participant=self.reader_membership,
            title="Linked Book",
            author="An Author",
            book_format=BookSubmission.Format.HARDCOVER,
            completed_on=date(2026, 8, 14),
            submitted_pages=300,
            reference_url="https://example.com/books/linked-edition",
        )
        self.client.force_login(self.mod)
        response = self.client.get(reverse("submission-review", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": submission.pk}))
        self.assertContains(response, 'href="https://example.com/books/linked-edition" target="_blank" rel="noopener noreferrer"')

    def test_start_date_cannot_be_after_completion_date(self):
        self.client.force_login(self.reader)
        response = self.client.post(reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), {
            "title": "Backwards Dates",
            "author": "An Author",
            "book_format": BookSubmission.Format.EBOOK,
            "started_on": "2026-08-20",
            "completed_on": "2026-08-12",
            "submitted_pages": 200,
            "reference_url": "",
            "notes": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Start date cannot be later than completion date.")
        self.assertFalse(BookSubmission.objects.filter(title="Backwards Dates").exists())

    def test_review_queue_month_name_links_back_to_month(self):
        self.client.force_login(self.mod)
        response = self.client.get(reverse("review-queue", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertContains(response, f'href="{self.month.get_absolute_url()}">{self.month.name}</a>')

    def test_cached_catalog_edition_is_snapshotted_onto_submission(self):
        book = CatalogBook.objects.create(provider="hardcover", provider_book_id="446680", title="Carl's Doomsday Scenario", author="Matt Dinniman", source_url="https://hardcover.app/books/carls-doomsday-scenario")
        edition = CatalogEdition.objects.create(provider="hardcover", provider_edition_id="30407787", book=book, format_name="Paperback", page_count=351, source_url="https://hardcover.app/books/carls-doomsday-scenario/editions/30407787")
        selection = signing.dumps({"selected": edition.pk, "scoring": edition.pk, "method": BookSubmission.VerificationMethod.HARDCOVER}, salt="northbound.catalog-selection")
        self.client.force_login(self.reader)
        response = self.client.post(reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), {
            "catalog_selection": selection,
            "title": "Tampered Title",
            "author": "Tampered Author",
            "book_format": BookSubmission.Format.OTHER,
            "started_on": "2026-08-01",
            "completed_on": "2026-08-12",
            "submitted_pages": 9999,
            "reference_url": "",
            "notes": "",
        })
        self.assertRedirects(response, self.month.get_absolute_url())
        submission = BookSubmission.objects.get(title="Carl's Doomsday Scenario")
        self.assertEqual(submission.catalog_book, book)
        self.assertEqual(submission.catalog_edition, edition)
        self.assertEqual(submission.metadata_pages, 351)
        self.assertEqual(submission.submitted_pages, 351)
        self.assertEqual(submission.approved_pages, 351)
        self.assertEqual(submission.status, BookSubmission.Status.APPROVED)
        self.assertEqual(submission.verification_method, BookSubmission.VerificationMethod.HARDCOVER)
        self.assertEqual(submission.reference_url, edition.source_url)

    @patch("core.views.search_books")
    def test_enrolled_reader_can_search_group_catalog(self, search):
        HardcoverConnection.objects.create(group=self.group, encrypted_token="encrypted", is_valid=True)
        search.return_value = ([{"book_id": "446680", "title": "Carl's Doomsday Scenario"}], False)
        self.client.force_login(self.reader)
        with patch("core.views.decrypt_token", return_value="token"):
            response = self.client.post(reverse("submission-catalog", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), {"action": "search", "query": "Carl"})
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"ok": True, "results": [{"book_id": "446680", "title": "Carl's Doomsday Scenario"}], "cached": False})
        search.assert_called_once_with("token", "Carl")


class ThemeScoringTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.reader = User.objects.create_user("theme-reader", password="test-password")
        self.moderator = User.objects.create_user("theme-moderator", password="test-password")
        self.group = ReadingGroup.objects.create(name="Theme Group", slug="theme-group")
        self.participant = Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Theme Reader")
        Membership.objects.create(group=self.group, user=self.moderator, role=Membership.Role.MODERATOR, display_name="Theme Moderator")
        self.month = ChallengeMonth.objects.create(group=self.group, name="August Themes", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.OPEN)
        MonthEnrollment.objects.create(month=self.month, participant=self.participant)
        self.prompted = MonthTheme.objects.create(month=self.month, name="Level Up", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), bonus_pages=50, prompt="Name the character that levels up")
        self.limited = MonthTheme.objects.create(month=self.month, name="Midmonth", starts_on=date(2026, 8, 10), ends_on=date(2026, 8, 20), bonus_pages=75)
        self.nonstacking = MonthTheme.objects.create(month=self.month, name="Solo", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), bonus_pages=100, allow_stacking=False)

    def submission_data(self, **overrides):
        data = {
            "title": "Theme Book",
            "author": "Theme Author",
            "book_format": BookSubmission.Format.EBOOK,
            "started_on": "2026-08-01",
            "completed_on": "2026-08-15",
            "submitted_pages": 300,
            "reference_url": "",
            "notes": "",
        }
        data.update(overrides)
        return data

    def test_hidden_and_inactive_themes_are_not_offered_or_accepted(self):
        hidden = MonthTheme.objects.create(month=self.month, name="Hidden", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), is_visible=False)
        inactive = MonthTheme.objects.create(month=self.month, name="Inactive", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), is_active=False)
        self.client.force_login(self.reader)
        page = self.client.get(reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertNotContains(page, "August Themes — Hidden")
        self.assertNotContains(page, "August Themes — Inactive")
        response = self.client.post(reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), self.submission_data(themes=[hidden.pk, inactive.pk]))
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(BookSubmission.objects.exists())

    def test_theme_completion_date_must_fall_inside_theme_window(self):
        self.client.force_login(self.reader)
        response = self.client.post(reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), self.submission_data(completed_on="2026-08-05", themes=[self.limited.pk]))
        self.assertContains(response, "Midmonth only applies from 2026-08-10 through 2026-08-20")
        self.assertFalse(BookSubmission.objects.exists())

    def test_selected_prompted_theme_requires_response(self):
        self.client.force_login(self.reader)
        response = self.client.post(reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), self.submission_data(themes=[self.prompted.pk]))
        self.assertContains(response, "Answer this prompt to claim the theme")
        self.assertFalse(BookSubmission.objects.exists())

    def test_nonstacking_theme_cannot_be_combined(self):
        self.client.force_login(self.reader)
        response = self.client.post(reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), self.submission_data(themes=[self.nonstacking.pk, self.limited.pk]))
        self.assertContains(response, "A selected theme cannot be stacked")
        self.assertFalse(BookSubmission.objects.exists())

    def test_valid_prompt_response_is_saved_with_pending_claim(self):
        self.client.force_login(self.reader)
        response = self.client.post(reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), self.submission_data(themes=[self.prompted.pk], **{f"theme_response_{self.prompted.pk}": "Carl"}))
        self.assertRedirects(response, self.month.get_absolute_url())
        claim = ThemeClaim.objects.get()
        self.assertEqual(claim.response, "Carl")
        self.assertEqual(claim.status, ThemeClaim.Status.PENDING)

    def test_claim_review_recalculates_bonus_without_changing_base_pages(self):
        submission = BookSubmission.objects.create(month=self.month, participant=self.participant, title="Reviewed Theme Book", author="Author", book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 15), submitted_pages=320, approved_pages=300, status=BookSubmission.Status.APPROVED)
        claim = ThemeClaim.objects.create(submission=submission, theme=self.prompted, response="Carl")
        self.client.force_login(self.moderator)
        response = self.client.post(reverse("submission-review", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": submission.pk}), {
            "approved_pages": 300,
            "status": BookSubmission.Status.APPROVED,
            "verification_url": "",
            "review_notes": "",
            "claims-TOTAL_FORMS": "1",
            "claims-INITIAL_FORMS": "1",
            "claims-MIN_NUM_FORMS": "0",
            "claims-MAX_NUM_FORMS": "1000",
            "claims-0-id": str(claim.pk),
            "claims-0-submission": str(submission.pk),
            "claims-0-status": ThemeClaim.Status.APPROVED,
        })
        self.assertRedirects(response, reverse("review-queue", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        submission.refresh_from_db()
        claim.refresh_from_db()
        self.assertEqual(submission.approved_pages, 300)
        self.assertEqual(submission.bonus_pages, 50)
        self.assertEqual(submission.final_scored_pages, 350)
        self.assertEqual(claim.approved_bonus_pages, 50)

    def test_rejected_submission_clears_bonus_but_preserves_approved_base_record(self):
        submission = BookSubmission.objects.create(month=self.month, participant=self.participant, title="Rejected Theme Book", author="Author", book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 15), submitted_pages=320, approved_pages=300, status=BookSubmission.Status.APPROVED)
        claim = ThemeClaim.objects.create(submission=submission, theme=self.prompted, response="Carl", status=ThemeClaim.Status.APPROVED, approved_bonus_pages=50)
        submission.recalculate_score()
        submission.status = BookSubmission.Status.REJECTED
        submission.save()
        claim.status = ThemeClaim.Status.REJECTED
        claim.approved_bonus_pages = 0
        claim.save()
        submission.recalculate_score()
        submission.refresh_from_db()
        self.assertEqual(submission.approved_pages, 300)
        self.assertEqual(submission.bonus_pages, 0)
        self.assertIsNone(submission.final_scored_pages)


class MonthLifecycleEnforcementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("lifecycle-owner", password="test-password")
        self.reader = User.objects.create_user("lifecycle-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Lifecycle Group", slug="lifecycle-group")
        Membership.objects.create(group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner")
        self.participant = Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Reader")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Lifecycle Month", starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 30), status=ChallengeMonth.Status.OPEN)
        MonthEnrollment.objects.create(month=self.month, participant=self.participant)
        self.submission = BookSubmission.objects.create(month=self.month, participant=self.participant, title="Pending Lifecycle Book", author="Author", book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 9, 10), submitted_pages=200)

    def set_status(self, status):
        self.month.status = status
        self.month.save(update_fields=["status"])

    def test_closed_month_allows_pending_review_but_blocks_configuration(self):
        self.set_status(ChallengeMonth.Status.CLOSED)
        self.client.force_login(self.owner)
        review = self.client.get(reverse("submission-review", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.submission.pk}))
        self.assertEqual(review.status_code, 200)
        add_team = self.client.post(reverse("team-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), {"name": "Late Team", "color": "#112233"})
        self.assertRedirects(add_team, self.month.get_absolute_url())
        self.assertFalse(Team.objects.filter(month=self.month, name="Late Team").exists())

    def test_finalized_month_blocks_reviews_and_roster_changes(self):
        self.set_status(ChallengeMonth.Status.FINALIZED)
        self.client.force_login(self.owner)
        review = self.client.get(reverse("submission-review", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.submission.pk}))
        self.assertRedirects(review, self.month.get_absolute_url())
        remove = self.client.post(reverse("month-participant-remove", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.month.enrollments.get().pk}), {"reason": "No"})
        self.assertRedirects(remove, self.month.get_absolute_url())
        self.assertTrue(MonthEnrollment.objects.filter(month=self.month, participant=self.participant).exists())

    def test_archived_month_cannot_be_reopened(self):
        self.set_status(ChallengeMonth.Status.ARCHIVED)
        self.client.force_login(self.owner)
        response = self.client.post(reverse("month-edit", kwargs={"group_slug": self.group.slug, "pk": self.month.pk}), {
            "name": self.month.name,
            "starts_on": "2026-09-01",
            "ends_on": "2026-09-30",
            "late_entry_deadline": "",
            "status": ChallengeMonth.Status.OPEN,
            "announcement_mode": ChallengeMonth.AnnouncementMode.INHERIT,
            "announcement": "",
        })
        self.assertRedirects(response, self.month.get_absolute_url())
        self.month.refresh_from_db()
        self.assertEqual(self.month.status, ChallengeMonth.Status.ARCHIVED)

    def test_finalized_month_can_only_advance_to_archived(self):
        self.set_status(ChallengeMonth.Status.FINALIZED)
        self.client.force_login(self.owner)
        response = self.client.post(reverse("month-edit", kwargs={"group_slug": self.group.slug, "pk": self.month.pk}), {
            "status": ChallengeMonth.Status.ARCHIVED,
            "announcement_mode": ChallengeMonth.AnnouncementMode.INHERIT,
        })
        self.assertRedirects(response, self.month.get_absolute_url())
        self.month.refresh_from_db()
        self.assertEqual(self.month.status, ChallengeMonth.Status.ARCHIVED)

    def test_closed_month_catalog_lookup_is_blocked_before_api_use(self):
        self.set_status(ChallengeMonth.Status.CLOSED)
        self.client.force_login(self.reader)
        response = self.client.post(reverse("submission-catalog", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), {"action": "search", "query": "Book"})
        self.assertEqual(response.status_code, 409)
        self.assertJSONEqual(response.content, {"ok": False, "message": "This challenge month is not open for submissions."})


class SubmissionPrivacyTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.reader_one = User.objects.create_user("privacy-reader-one", password="test-password")
        self.reader_two = User.objects.create_user("privacy-reader-two", password="test-password")
        self.moderator = User.objects.create_user("privacy-moderator", password="test-password")
        self.group = ReadingGroup.objects.create(name="Privacy Group", slug="privacy-group")
        self.membership_one = Membership.objects.create(group=self.group, user=self.reader_one, role=Membership.Role.READER, display_name="Reader One")
        self.membership_two = Membership.objects.create(group=self.group, user=self.reader_two, role=Membership.Role.READER, display_name="Reader Two")
        Membership.objects.create(group=self.group, user=self.moderator, role=Membership.Role.MODERATOR, display_name="Moderator")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Private Month", starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 31), status=ChallengeMonth.Status.OPEN)
        self.own_submission = BookSubmission.objects.create(month=self.month, participant=self.membership_one, title="My Visible Book", author="Author One", book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 10, 5), submitted_pages=210)
        self.other_submission = BookSubmission.objects.create(month=self.month, participant=self.membership_two, title="Other Secret Book", author="Author Two", book_format=BookSubmission.Format.PAPERBACK, completed_on=date(2026, 10, 6), submitted_pages=987)

    def test_reader_sees_only_own_submissions(self):
        self.client.force_login(self.reader_one)
        response = self.client.get(self.month.get_absolute_url())
        self.assertContains(response, "My Submissions")
        self.assertContains(response, "My Visible Book")
        self.assertNotContains(response, "Other Secret Book")
        self.assertNotContains(response, "987")
        self.assertNotContains(response, "Reader Two")

    def test_moderator_sees_all_month_submissions(self):
        self.client.force_login(self.moderator)
        response = self.client.get(self.month.get_absolute_url())
        self.assertContains(response, "Recent Submissions")
        self.assertContains(response, "My Visible Book")
        self.assertContains(response, "Other Secret Book")
        self.assertContains(response, "Reader One")
        self.assertContains(response, "Reader Two")


class PlatformRootVisibilityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.root = User.objects.create_superuser("rootdev", "root@example.com", "test-password")
        self.reader = User.objects.create_user("visible-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Visible Group", slug="visible-group")
        Membership.objects.create(group=self.group, user=self.root, role=Membership.Role.OWNER, display_name="Hidden Root")
        self.reader_membership = Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Visible Reader")

    def test_root_membership_is_hidden_from_participant_directory_and_count(self):
        self.client.force_login(self.root)
        group_page = self.client.get(reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.assertContains(group_page, "Participants")
        self.assertNotContains(group_page, "Hidden Root")

        participant_page = self.client.get(reverse("participant-list", kwargs={"group_slug": self.group.slug}))
        self.assertContains(participant_page, "Visible Reader")
        self.assertNotContains(participant_page, "Hidden Root")

    def test_root_can_adjust_other_roles(self):
        self.client.force_login(self.root)
        response = self.client.post(reverse("participant-role-edit", kwargs={"group_slug": self.group.slug, "pk": self.reader_membership.pk}), {
            "role": Membership.Role.MODERATOR,
            "is_active": "on",
        })
        self.assertRedirects(response, reverse("participant-list", kwargs={"group_slug": self.group.slug}))
        self.reader_membership.refresh_from_db()
        self.assertEqual(self.reader_membership.role, Membership.Role.MODERATOR)

    def test_root_can_deactivate_participant_without_deleting_history(self):
        self.client.force_login(self.root)
        response = self.client.post(reverse("participant-deactivate", kwargs={"group_slug": self.group.slug, "pk": self.reader_membership.pk}), {"reason": "Test removal"})
        self.assertRedirects(response, reverse("participant-list", kwargs={"group_slug": self.group.slug}))
        self.reader_membership.refresh_from_db()
        self.assertFalse(self.reader_membership.is_active)


class AccountAndConfigurationAccessTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.root = User.objects.create_superuser("platform-root", "root@example.com", "root-test-password-482!")
        self.reader = User.objects.create_user("account-reader", "reader@example.com", "reader-test-password-482!")

    def test_regular_user_cannot_use_platform_owner_login(self):
        response = self.client.post(reverse("config-login"), {
            "username": "account-reader",
            "password": "reader-test-password-482!",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not authorized for platform owner access")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_root_cannot_use_regular_login(self):
        response = self.client.post(reverse("login"), {
            "username": "platform-root",
            "password": "root-test-password-482!",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must use the separate owner sign-in")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_regular_authenticated_user_cannot_open_configuration_center(self):
        self.client.force_login(self.reader)
        response = self.client.get(reverse("config-dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_root_login_opens_configuration_center_and_is_audited(self):
        response = self.client.post(reverse("config-login"), {
            "username": "platform-root",
            "password": "root-test-password-482!",
        })
        self.assertRedirects(response, reverse("config-dashboard"))
        response = self.client.get(reverse("config-dashboard"))
        self.assertContains(response, "Platform Administration")
        from .models import AuditEvent
        self.assertTrue(AuditEvent.objects.filter(actor=self.root, action="platform.root_login").exists())

    def test_owner_can_generate_and_redeem_one_time_invitation(self):
        self.client.force_login(self.root)
        response = self.client.post(reverse("platform-owner-create"), {
            "current_password": "root-test-password-482!",
        })
        self.assertEqual(response.status_code, 200)
        invitation = PlatformOwnerInvitation.objects.get()
        invitation_url = response.context["invitation_url"]
        token = invitation_url.rstrip("/").rsplit("/", 1)[-1]
        self.assertNotEqual(invitation.token_hash, token)
        self.assertAlmostEqual(
            invitation.expires_at,
            timezone.now() + timedelta(days=7),
            delta=timedelta(seconds=5),
        )
        self.assertTrue(AuditEvent.objects.filter(
            actor=self.root,
            action="platform.owner_invitation_created",
            object_id=str(invitation.pk),
        ).exists())

        self.client.logout()
        accept_url = reverse("platform-owner-accept", kwargs={"token": token})
        response = self.client.post(accept_url, {
            "username": "second-owner",
            "email": "second-owner@example.com",
            "password1": "second-owner-test-password-739!",
            "password2": "second-owner-test-password-739!",
        })
        self.assertRedirects(response, reverse("config-dashboard"))
        second_owner = get_user_model().objects.get(username="second-owner")
        self.assertTrue(second_owner.is_superuser)
        self.assertTrue(second_owner.is_staff)
        self.assertFalse(Membership.objects.filter(user=second_owner).exists())
        invitation.refresh_from_db()
        self.assertEqual(invitation.redeemed_by, second_owner)
        self.assertIsNotNone(invitation.redeemed_at)
        self.assertTrue(AuditEvent.objects.filter(
            actor=second_owner,
            action="platform.owner_invitation_redeemed",
            object_id=str(invitation.pk),
        ).exists())
        self.client.logout()
        self.assertEqual(self.client.get(accept_url).status_code, 410)

    def test_generating_invitation_requires_current_owner_password(self):
        self.client.force_login(self.root)
        response = self.client.post(reverse("platform-owner-create"), {
            "current_password": "wrong-password",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your current password is incorrect")
        self.assertFalse(PlatformOwnerInvitation.objects.exists())

    def test_owner_can_revoke_unused_invitation(self):
        invitation, token = PlatformOwnerInvitation.issue(self.root)
        self.client.force_login(self.root)
        response = self.client.post(reverse("platform-owner-invitation-revoke", kwargs={"pk": invitation.pk}))
        self.assertRedirects(response, reverse("platform-owner-list"))
        invitation.refresh_from_db()
        self.assertEqual(invitation.revoked_by, self.root)
        self.assertIsNotNone(invitation.revoked_at)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("platform-owner-accept", kwargs={"token": token})).status_code, 410)

    def test_expired_invitation_cannot_be_redeemed(self):
        invitation, token = PlatformOwnerInvitation.issue(self.root)
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save(update_fields=["expires_at"])
        response = self.client.get(reverse("platform-owner-accept", kwargs={"token": token}))
        self.assertEqual(response.status_code, 410)

    def test_owner_can_deactivate_and_reactivate_another_owner_with_password_confirmation(self):
        second_owner = get_user_model().objects.create_superuser(
            "lifecycle-owner", "lifecycle@example.com", "lifecycle-owner-password-739!"
        )
        historical_event = AuditEvent.objects.create(
            actor=second_owner,
            action="platform.lifecycle_history",
            object_type="User",
            object_id=str(second_owner.pk),
            summary="Historical owner activity.",
        )
        self.client.force_login(self.root)
        status_url = reverse("platform-owner-status-toggle", kwargs={"pk": second_owner.pk})
        listing = self.client.get(reverse("platform-owner-list"))
        self.assertContains(listing, status_url)

        response = self.client.post(status_url, {"current_password": "root-test-password-482!"})
        self.assertRedirects(response, reverse("platform-owner-list"))
        second_owner.refresh_from_db()
        self.assertFalse(second_owner.is_active)
        self.assertTrue(get_user_model().objects.filter(pk=second_owner.pk, is_superuser=True).exists())
        historical_event.refresh_from_db()
        self.assertEqual(historical_event.actor, second_owner)
        self.assertTrue(AuditEvent.objects.filter(
            actor=self.root,
            action="platform.owner_deactivated",
            object_id=str(second_owner.pk),
        ).exists())

        response = self.client.post(status_url, {"current_password": "root-test-password-482!"})
        self.assertRedirects(response, reverse("platform-owner-list"))
        second_owner.refresh_from_db()
        self.assertTrue(second_owner.is_active)
        self.assertTrue(AuditEvent.objects.filter(
            actor=self.root,
            action="platform.owner_reactivated",
            object_id=str(second_owner.pk),
        ).exists())

    def test_owner_status_change_rejects_wrong_current_password(self):
        second_owner = get_user_model().objects.create_superuser(
            "protected-owner", "protected@example.com", "protected-owner-password-739!"
        )
        self.client.force_login(self.root)
        response = self.client.post(
            reverse("platform-owner-status-toggle", kwargs={"pk": second_owner.pk}),
            {"current_password": "wrong-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your current password is incorrect")
        second_owner.refresh_from_db()
        self.assertTrue(second_owner.is_active)
        self.assertFalse(AuditEvent.objects.filter(action="platform.owner_deactivated").exists())

    def test_owner_cannot_deactivate_self_or_leave_no_active_owner(self):
        self.client.force_login(self.root)
        response = self.client.post(
            reverse("platform-owner-status-toggle", kwargs={"pk": self.root.pk}),
            {"current_password": "root-test-password-482!"},
        )
        self.assertRedirects(response, reverse("platform-owner-list"))
        self.root.refresh_from_db()
        self.assertTrue(self.root.is_active)
        self.assertEqual(
            get_user_model().objects.filter(is_superuser=True, is_active=True).count(),
            1,
        )
        self.assertFalse(AuditEvent.objects.filter(action="platform.owner_deactivated").exists())

    def test_regular_user_cannot_manage_platform_owners(self):
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(reverse("platform-owner-list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("platform-owner-create")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("platform-owner-status-toggle", kwargs={"pk": self.root.pk})).status_code,
            403,
        )

    def test_user_can_update_account_and_change_password(self):
        self.client.force_login(self.reader)
        response = self.client.post(reverse("account"), {
            "username": "account-reader",
            "first_name": "Reader",
            "last_name": "One",
            "email": "updated-reader@example.com",
        })
        self.assertRedirects(response, reverse("account"))
        self.reader.refresh_from_db()
        self.assertEqual(self.reader.email, "updated-reader@example.com")

        response = self.client.post(reverse("password-change"), {
            "old_password": "reader-test-password-482!",
            "new_password1": "new-reader-test-password-739!",
            "new_password2": "new-reader-test-password-739!",
        })
        self.assertRedirects(response, reverse("account"))
        self.reader.refresh_from_db()
        self.assertTrue(self.reader.check_password("new-reader-test-password-739!"))
        self.assertIn("_auth_user_id", self.client.session)


class PlatformSettingsTests(TransactionTestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.data_root = tempfile.mkdtemp()
        self.data_root_patch = patch("core.backups.data_root", return_value=Path(self.data_root))
        self.data_root_patch.start()
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()
        self.owner = get_user_model().objects.create_superuser(
            "backup-owner", "backup@example.com", "backup-test-password-482!"
        )

    def tearDown(self):
        self.override.disable()
        self.data_root_patch.stop()
        shutil.rmtree(self.media_root, ignore_errors=True)
        shutil.rmtree(self.data_root, ignore_errors=True)

    def test_owner_can_create_and_then_download_stored_manual_backup(self):
        with open(f"{self.media_root}/profile-picture.txt", "wb") as media_file:
            media_file.write(b"profile-picture")
        Path(self.data_root, ".env").write_text("DJANGO_SECRET_KEY=must-not-be-backed-up")
        self.client.force_login(self.owner)
        response = self.client.post(reverse("platform-backup-create"))
        self.assertRedirects(response, reverse("platform-backups"))
        backup_path = next(Path(self.data_root, "backups").glob("northbound-manual-*.zip"))
        with zipfile.ZipFile(backup_path) as backup_zip:
            self.assertIn("northbound.sqlite3", backup_zip.namelist())
            self.assertIn("media/profile-picture.txt", backup_zip.namelist())
            self.assertIn("northbound-backup.json", backup_zip.namelist())
            self.assertNotIn(".env", backup_zip.namelist())
        self.assertTrue(AuditEvent.objects.filter(action="platform.backup_created", actor=self.owner).exists())

        listing = self.client.get(reverse("platform-backups"))
        self.assertContains(listing, "Stored Backups")
        self.assertContains(listing, "Manual")
        self.assertContains(listing, reverse("stored-backup-restore", kwargs={"filename": backup_path.name}))
        self.assertContains(listing, reverse("stored-backup-download", kwargs={"filename": backup_path.name}))
        self.assertContains(listing, reverse("stored-backup-delete", kwargs={"filename": backup_path.name}))

        download = self.client.get(reverse("stored-backup-download", kwargs={"filename": backup_path.name}))
        self.assertEqual(download.status_code, 200)
        self.assertGreater(len(b"".join(download.streaming_content)), 0)
        self.assertTrue(AuditEvent.objects.filter(action="platform.backup_downloaded", actor=self.owner).exists())

    def test_stored_backup_delete_requires_confirmation(self):
        from .backups import create_stored_backup

        backup_path = create_stored_backup()
        self.client.force_login(self.owner)
        url = reverse("stored-backup-delete", kwargs={"filename": backup_path.name})
        confirmation = self.client.get(url)
        self.assertContains(confirmation, "Delete This Backup?")
        self.assertTrue(backup_path.exists())
        response = self.client.post(url)
        self.assertRedirects(response, reverse("platform-backups"))
        self.assertFalse(backup_path.exists())
        self.assertTrue(AuditEvent.objects.filter(action="platform.backup_deleted", actor=self.owner).exists())

    @override_settings(NORTHBOUND_WEB_RESTART=False)
    def test_stored_backup_restore_requires_password_and_restore_confirmation(self):
        from .backups import create_stored_backup, pending_restore_path

        backup_path = create_stored_backup()
        self.client.force_login(self.owner)
        url = reverse("stored-backup-restore", kwargs={"filename": backup_path.name})
        response = self.client.post(url, {
            "current_password": "wrong-password",
            "confirmation": "RESTORE",
        })
        self.assertContains(response, "Your current password is incorrect")
        self.assertFalse(pending_restore_path().exists())
        response = self.client.post(url, {
            "current_password": "backup-test-password-482!",
            "confirmation": "not-restore",
        })
        self.assertContains(response, "Enter RESTORE exactly")
        self.assertFalse(pending_restore_path().exists())
        response = self.client.post(url, {
            "current_password": "backup-test-password-482!",
            "confirmation": "RESTORE",
        })
        self.assertRedirects(response, reverse("platform-backups"))
        self.assertTrue(pending_restore_path().exists())
        self.assertTrue(AuditEvent.objects.filter(action="platform.restore_staged", actor=self.owner).exists())

    def test_automatic_backup_defaults_and_schedule_are_configurable(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("platform-backups"))
        self.assertEqual(response.status_code, 200)
        backup_settings = PlatformBackupSettings.load()
        self.assertTrue(backup_settings.enabled)
        self.assertEqual(backup_settings.weekdays, [PlatformBackupSettings.Weekday.MONDAY])
        self.assertEqual(backup_settings.backup_time.hour, 1)
        self.assertEqual(backup_settings.retention_count, 5)
        response = self.client.post(reverse("platform-backups"), {
            "enabled": "on",
            "weekdays": [PlatformBackupSettings.Weekday.MONDAY, PlatformBackupSettings.Weekday.FRIDAY],
            "backup_time": "03:30",
            "retention_count": 9,
        })
        self.assertRedirects(response, reverse("platform-backups"))
        backup_settings.refresh_from_db()
        self.assertEqual(backup_settings.weekdays, [PlatformBackupSettings.Weekday.MONDAY, PlatformBackupSettings.Weekday.FRIDAY])
        self.assertEqual(backup_settings.backup_time.hour, 3)
        self.assertEqual(backup_settings.retention_count, 9)

    def test_retention_removes_only_oldest_automatic_backups(self):
        backup_directory = Path(self.data_root, "backups")
        backup_directory.mkdir()
        manual = backup_directory / "northbound-manual-20260823-010000-000000.zip"
        automatic_old = backup_directory / "northbound-automatic-20260821-010000-000000.zip"
        automatic_new = backup_directory / "northbound-automatic-20260822-010000-000000.zip"
        for index, path in enumerate((automatic_old, automatic_new, manual), start=1):
            path.write_bytes(b"backup")
            os.utime(path, (index, index))
        self.client.force_login(self.owner)
        response = self.client.post(reverse("platform-backups"), {
            "enabled": "on",
            "weekdays": [PlatformBackupSettings.Weekday.MONDAY],
            "backup_time": "01:00",
            "retention_count": 1,
        })
        self.assertRedirects(response, reverse("platform-backups"))
        self.assertFalse(automatic_old.exists())
        self.assertTrue(automatic_new.exists())
        self.assertTrue(manual.exists())

    def test_scheduler_records_success_and_latest_failure(self):
        from django.core.management import call_command

        backup_settings = PlatformBackupSettings.load()
        local_now = timezone.localtime()
        backup_settings.weekdays = [local_now.weekday()]
        backup_settings.backup_time = (local_now - timedelta(minutes=1)).time().replace(tzinfo=None)
        backup_settings.last_run_date = None
        backup_settings.save()
        with patch("core.management.commands.run_backup_scheduler.create_automatic_backup", return_value=Path("backup.zip")):
            call_command("run_backup_scheduler", "--once")
        backup_settings.refresh_from_db()
        self.assertIsNotNone(backup_settings.last_success_at)

        backup_settings.last_run_date = None
        backup_settings.save(update_fields=["last_run_date"])
        with patch("core.management.commands.run_backup_scheduler.create_automatic_backup", side_effect=OSError("disk full")):
            call_command("run_backup_scheduler", "--once")
        backup_settings.refresh_from_db()
        self.assertIsNotNone(backup_settings.last_failure_at)
        self.assertEqual(backup_settings.last_error, "disk full")
        self.assertIsNone(backup_settings.last_run_date)

    def test_all_weekdays_produce_a_daily_next_run_and_status_is_exposed(self):
        backup_settings = PlatformBackupSettings.load()
        backup_settings.weekdays = list(range(7))
        backup_settings.backup_time = (timezone.localtime() + timedelta(minutes=1)).time().replace(tzinfo=None)
        backup_settings.last_success_at = timezone.now() - timedelta(hours=2)
        backup_settings.last_failure_at = timezone.now() - timedelta(hours=1)
        backup_settings.last_error = "Example failure"
        backup_settings.save()
        self.client.force_login(self.owner)
        response = self.client.get(reverse("platform-backups"))
        self.assertIsNotNone(response.context["next_scheduled_run"])
        self.assertContains(response, str(Path(self.data_root, "backups")))
        self.assertContains(response, "Example failure")

    def test_backup_validation_rejects_invalid_metadata_and_unexpected_files(self):
        from .backups import create_stored_backup, validate_backup

        valid_backup = create_stored_backup()
        with zipfile.ZipFile(valid_backup, "a") as backup_zip:
            backup_zip.writestr(".env", "DJANGO_SECRET_KEY=unexpected")
        with self.assertRaisesMessage(ValueError, "unexpected file"):
            validate_backup(valid_backup)

        invalid_metadata = Path(self.data_root, "invalid-metadata.zip")
        with zipfile.ZipFile(invalid_metadata, "w") as backup_zip:
            backup_zip.writestr("northbound.sqlite3", b"not-reached")
            backup_zip.writestr("northbound-backup.json", '{"database": "sqlite"}')
        with self.assertRaisesMessage(ValueError, "invalid creation time"):
            validate_backup(invalid_metadata)


class PlatformSystemStatusTests(TestCase):
    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.owner = get_user_model().objects.create_superuser(
            "status-owner", "status@example.com", "status-owner-password-482!"
        )
        self.reader = get_user_model().objects.create_user(
            "status-reader", "reader@example.com", "reader-password-482!"
        )

    def test_platform_owner_sees_read_only_status_without_secrets(self):
        backup_settings = PlatformBackupSettings.load()
        backup_settings.last_success_at = timezone.now() - timedelta(hours=1)
        backup_settings.save(update_fields=["last_success_at"])
        with override_settings(
            NORTHBOUND_VERSION="2026.8-test",
            NORTHBOUND_URL="https://northbound.example.com",
            NORTHBOUND_TRUST_PROXY_HEADERS=True,
            TIME_ZONE="America/New_York",
            MEDIA_ROOT=Path(self.media_directory.name),
            DEBUG=False,
            SECRET_KEY="system-status-secret-key",
            TOKEN_ENCRYPTION_KEY="system-status-token-key",
        ):
            self.client.force_login(self.owner)
            response = self.client.get(reverse("platform-system-status"))
            settings_page = self.client.get(reverse("platform-settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2026.8-test")
        self.assertContains(response, "SQLite")
        self.assertContains(response, "America/New_York")
        self.assertContains(response, "https://northbound.example.com")
        self.assertContains(response, "Trusted proxy headers enabled")
        self.assertContains(response, str(Path(self.media_directory.name).resolve()))
        self.assertContains(response, "Migration State")
        self.assertContains(response, "Up to date")
        self.assertContains(response, "Backup Scheduler")
        self.assertContains(response, "Successful")
        self.assertContains(response, "Storage Availability")
        self.assertNotContains(response, "system-status-secret-key")
        self.assertNotContains(response, "system-status-token-key")
        self.assertNotContains(response, "Restart Northbound")
        self.assertNotContains(response, "Run Migrations")
        self.assertNotContains(response, "Repair Database")

        self.assertContains(settings_page, reverse("platform-system-status"))

    def test_regular_account_cannot_access_system_status(self):
        self.client.force_login(self.reader)
        response = self.client.get(reverse("platform-system-status"))
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "Database Location", status_code=403)

    def test_actionable_warnings_cover_proxy_debug_scheduler_and_backup_failure(self):
        backup_settings = PlatformBackupSettings.load()
        backup_settings.enabled = True
        backup_settings.last_success_at = timezone.now() - timedelta(days=1)
        backup_settings.last_failure_at = timezone.now()
        backup_settings.last_error = "sensitive implementation detail remains on Backups only"
        backup_settings.save()
        self.client.force_login(self.owner)
        with override_settings(
            NORTHBOUND_VERSION="development",
            NORTHBOUND_URL="https://northbound.example.com",
            NORTHBOUND_TRUST_PROXY_HEADERS=False,
            MEDIA_ROOT=Path(self.media_directory.name),
            DEBUG=True,
        ):
            response = self.client.get(reverse("platform-system-status"))

        self.assertContains(response, "Development build")
        self.assertContains(response, "HTTPS proxy headers are not trusted")
        self.assertContains(response, "Debug mode is enabled")
        self.assertContains(response, "The latest automatic backup failed")
        self.assertContains(response, "Failed — review Settings → Backups")
        self.assertNotContains(response, "sensitive implementation detail")

    @patch("core.system_status.MigrationExecutor")
    def test_pending_migrations_are_reported_without_running_them(self, executor_class):
        executor = executor_class.return_value
        executor.loader.graph.leaf_nodes.return_value = [("core", "0025_backup_operational_status")]
        executor.migration_plan.return_value = [object(), object()]
        self.client.force_login(self.owner)
        with override_settings(
            NORTHBOUND_VERSION="2026.8-test",
            NORTHBOUND_URL="http://localhost:8000",
            MEDIA_ROOT=Path(self.media_directory.name),
            DEBUG=False,
        ):
            response = self.client.get(reverse("platform-system-status"))

        self.assertContains(response, "2 pending")
        self.assertContains(response, "Database migrations are pending")
        executor.migration_plan.assert_called_once()

    @patch("core.system_status._migration_status", return_value={"label": "Up to date", "pending_count": 0})
    @patch("core.system_status.connection")
    def test_postgresql_status_never_displays_database_credentials(self, status_connection, _migration_status):
        from .system_status import build_system_status

        status_connection.vendor = "postgresql"
        status_connection.settings_dict = {
            "HOST": "database.internal",
            "PORT": "5432",
            "NAME": "northbound",
            "USER": "private-database-user",
            "PASSWORD": "private-database-password",
        }
        with override_settings(
            NORTHBOUND_VERSION="2026.8-test",
            NORTHBOUND_URL="http://localhost:8000",
            NORTHBOUND_TRUST_PROXY_HEADERS=False,
            MEDIA_ROOT=Path(self.media_directory.name),
            DEBUG=False,
        ):
            status = build_system_status()

        self.assertEqual(status["database_backend"], "PostgreSQL")
        self.assertEqual(status["database_location"], "database.internal:5432 / northbound")
        rendered_values = repr(status)
        self.assertNotIn("private-database-user", rendered_values)
        self.assertNotIn("private-database-password", rendered_values)


class PlatformAuditActivityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser(
            "audit-owner", "audit-owner@example.com", "audit-owner-password-482!"
        )
        self.reader = User.objects.create_user(
            "audit-reader", "audit-reader@example.com", "audit-reader-password-482!"
        )
        self.deactivated_actor = User.objects.create_user(
            "former-operator", "former@example.com", "former-password-482!", is_active=False
        )
        self.group = ReadingGroup.objects.create(name="Audit Group", slug="audit-group")
        self.other_group = ReadingGroup.objects.create(name="Other Group", slug="other-audit-group")

    def create_event(self, *, actor=None, group=None, action="group.updated", summary="Audit event", created_at=None):
        event = AuditEvent.objects.create(
            actor=actor,
            group=group,
            action=action,
            object_type="ReadingGroup",
            object_id=str(group.pk) if group else "",
            summary=summary,
        )
        if created_at:
            AuditEvent.objects.filter(pk=event.pk).update(created_at=created_at)
            event.refresh_from_db()
        return event

    @override_settings(TIME_ZONE="America/New_York")
    def test_filters_combine_and_use_platform_local_date(self):
        target = self.create_event(
            actor=self.deactivated_actor,
            group=self.group,
            action="group.updated",
            summary="Resolved support case Alpha",
            created_at=datetime(2026, 8, 24, 2, 0, tzinfo=datetime_timezone.utc),
        )
        self.create_event(
            actor=self.deactivated_actor,
            group=self.other_group,
            action="group.updated",
            summary="Resolved support case Alpha in another group",
            created_at=datetime(2026, 8, 24, 2, 0, tzinfo=datetime_timezone.utc),
        )
        self.create_event(
            actor=self.deactivated_actor,
            group=self.group,
            action="group.created",
            summary="Resolved support case Alpha with another action",
            created_at=datetime(2026, 8, 24, 2, 0, tzinfo=datetime_timezone.utc),
        )
        self.client.force_login(self.owner)
        response = self.client.get(reverse("config-audit"), {
            "search": "support case Alpha",
            "action": "group.updated",
            "actor": str(self.deactivated_actor.pk),
            "group": str(self.group.pk),
            "date": "2026-08-23",
        })

        self.assertEqual(list(response.context["page_obj"].object_list), [target])
        self.assertContains(response, "former-operator")
        self.assertContains(response, "Deactivated")
        self.assertContains(response, "Group Updated")
        self.assertContains(response, "group.updated")
        self.assertContains(response, "Clear Filters")

    def test_summary_is_sanitized_without_mutating_historical_record(self):
        event = self.create_event(
            actor=self.owner,
            summary="Investigated API_TOKEN=do-not-display and DJANGO_SECRET_KEY=also-private for support.",
        )
        self.client.force_login(self.owner)
        response = self.client.get(reverse("config-audit"))

        self.assertContains(response, "API_TOKEN=[REDACTED]")
        self.assertNotContains(response, "do-not-display")
        self.assertContains(response, "DJANGO_SECRET_KEY=[REDACTED]")
        self.assertNotContains(response, "also-private")
        event.refresh_from_db()
        self.assertIn("do-not-display", event.summary)

    def test_server_side_pagination_preserves_search(self):
        for number in range(51):
            self.create_event(actor=self.owner, summary=f"Paged support record {number}")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("config-audit"), {"search": "Paged support record"})

        self.assertEqual(response.context["page_obj"].paginator.count, 51)
        self.assertEqual(len(response.context["events"]), 50)
        self.assertContains(response, "Page 1 of 2")
        self.assertContains(response, "search=Paged+support+record&amp;page=2")

    def test_filtered_csv_export_contains_safe_friendly_and_stable_fields(self):
        self.create_event(
            actor=self.deactivated_actor,
            group=self.group,
            action="platform.root_login",
            summary="Support archive API_TOKEN=never-export-this",
        )
        self.create_event(actor=self.owner, action="group.created", summary="Unrelated event")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("config-audit-export"), {
            "search": "Support archive",
            "action": "platform.root_login",
        })
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("northbound-audit-activity-", response["Content-Disposition"])
        self.assertIn("Platform Owner Signed In", content)
        self.assertIn("platform.root_login", content)
        self.assertIn("former-operator", content)
        self.assertIn("Audit Group", content)
        self.assertIn("API_TOKEN=[REDACTED]", content)
        self.assertNotIn("never-export-this", content)
        self.assertNotIn("Unrelated event", content)

    def test_audit_page_and_export_are_platform_owner_only(self):
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(reverse("config-audit")).status_code, 403)
        self.assertEqual(self.client.get(reverse("config-audit-export")).status_code, 403)


class GeneralSettingsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser(
            "settings-owner", "settings-owner@example.com", "settings-owner-password-482!"
        )
        self.reader = User.objects.create_user(
            "settings-reader", "settings-reader@example.com", "settings-reader-password-482!"
        )
        self.group = ReadingGroup.objects.create(
            name="Existing Group", slug="existing-settings-group", timezone="America/Chicago"
        )

    def test_owner_updates_all_runtime_safe_settings_with_audit_history(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("platform-general-settings"), {
            "display_name": "Deep North Readers",
            "timezone": "America/Los_Angeles",
        })

        self.assertRedirects(response, reverse("platform-general-settings"))
        platform_settings = PlatformSettings.load()
        self.assertEqual(platform_settings.display_name, "Deep North Readers")
        self.assertEqual(platform_settings.timezone, "America/Los_Angeles")
        self.assertFalse(platform_settings.allow_public_registration)
        self.assertFalse(platform_settings.allow_user_group_creation)
        self.group.refresh_from_db()
        self.assertEqual(self.group.timezone, "America/Chicago")

        event = AuditEvent.objects.get(action="platform.general_settings_updated")
        self.assertEqual(event.actor, self.owner)
        self.assertIn("Platform display name changed from My Northbound to Deep North Readers", event.summary)
        self.assertIn("Platform timezone changed from America/New_York to America/Los_Angeles", event.summary)
        self.assertIn("Public registration changed from Enabled to Disabled", event.summary)
        self.assertIn("Normal account group creation changed from Enabled to Disabled", event.summary)

        response = self.client.get(reverse("config-dashboard"))
        self.assertContains(response, "Deep North Readers")
        self.client.logout()
        self.assertContains(self.client.get(reverse("login")), "Deep North Readers")

    def test_invalid_timezone_is_rejected_and_regular_accounts_cannot_manage_settings(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("platform-general-settings"), {
            "display_name": "Invalid Timezone Test",
            "timezone": "Mars/Olympus_Mons",
            "allow_public_registration": "on",
            "allow_user_group_creation": "on",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(AuditEvent.objects.filter(action="platform.general_settings_updated").exists())

        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(reverse("platform-general-settings")).status_code, 403)
        self.assertEqual(self.client.post(reverse("platform-general-settings"), {}).status_code, 403)

    def test_disabled_registration_blocks_direct_access_but_existing_accounts_still_sign_in(self):
        platform_settings = PlatformSettings.load()
        platform_settings.allow_public_registration = False
        platform_settings.save(update_fields=["allow_public_registration"])

        response = self.client.get(reverse("register"))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Registration Unavailable", status_code=403)
        response = self.client.post(reverse("register"), {
            "username": "blocked-registration",
            "email": "blocked@example.com",
            "password1": "blocked-registration-password-482!",
            "password2": "blocked-registration-password-482!",
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(get_user_model().objects.filter(username="blocked-registration").exists())
        self.assertTrue(self.client.login(username=self.reader.username, password="settings-reader-password-482!"))

    def test_group_creation_policy_blocks_normal_accounts_but_preserves_joining_and_owner_creation(self):
        platform_settings = PlatformSettings.load()
        platform_settings.allow_user_group_creation = False
        platform_settings.save(update_fields=["allow_user_group_creation"])
        self.group.regenerate_access_code()
        self.group.save(update_fields=["join_code", "join_code_hash", "join_code_hint"])

        self.client.force_login(self.reader)
        dashboard = self.client.get(reverse("dashboard"))
        self.assertNotContains(dashboard, reverse("group-create"))
        self.assertContains(dashboard, reverse("group-join"))
        self.assertEqual(self.client.get(reverse("group-create")).status_code, 403)
        response = self.client.post(reverse("group-create"), {
            "name": "Blocked Group",
            "timezone": "America/New_York",
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ReadingGroup.objects.filter(name="Blocked Group").exists())

        response = self.client.post(reverse("group-join"), {"access_code": self.group.join_code})
        self.assertRedirects(response, reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.assertTrue(Membership.objects.filter(group=self.group, user=self.reader, is_active=True).exists())

        self.client.force_login(self.owner)
        response = self.client.post(reverse("group-create"), {
            "name": "Platform Created Group",
            "timezone": "America/New_York",
        })
        platform_group = ReadingGroup.objects.get(name="Platform Created Group")
        self.assertRedirects(response, reverse("config-group-detail", kwargs={"group_slug": platform_group.slug}))
        self.assertFalse(Membership.objects.filter(group=platform_group, user=self.owner).exists())

    @override_settings(TIME_ZONE="UTC")
    def test_runtime_timezone_drives_audit_backups_and_system_status(self):
        from .backups import next_scheduled_backup

        platform_settings = PlatformSettings.load()
        platform_settings.timezone = "America/Los_Angeles"
        platform_settings.save(update_fields=["timezone"])
        event = AuditEvent.objects.create(
            actor=self.owner,
            action="platform.root_login",
            object_type="User",
            object_id=str(self.owner.pk),
            summary="Timezone integration event",
        )
        AuditEvent.objects.filter(pk=event.pk).update(
            created_at=datetime(2026, 8, 24, 2, 0, tzinfo=datetime_timezone.utc)
        )

        self.client.force_login(self.owner)
        audit_response = self.client.get(reverse("config-audit"), {"date": "2026-08-23"})
        self.assertContains(audit_response, "Timezone integration event")
        status_response = self.client.get(reverse("platform-system-status"))
        self.assertContains(status_response, "America/Los_Angeles")
        backup_response = self.client.get(reverse("platform-backups"))
        self.assertEqual(backup_response.context["platform_timezone"], "America/Los_Angeles")

        backup_settings = PlatformBackupSettings.load()
        backup_settings.weekdays = [PlatformBackupSettings.Weekday.MONDAY]
        backup_settings.backup_time = datetime(2026, 8, 24, 1, 0).time()
        next_run = next_scheduled_backup(
            backup_settings,
            now=datetime(2026, 8, 24, 7, 30, tzinfo=datetime_timezone.utc),
        )
        self.assertEqual(getattr(next_run.tzinfo, "key", None), "America/Los_Angeles")
        self.assertEqual(next_run.hour, 1)


class GroupEditingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("group-owner", password="test-password")
        self.reader = User.objects.create_user("group-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Original Group", slug="original-group", timezone="America/New_York")
        Membership.objects.create(group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner")
        Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Reader")

    def test_owner_can_edit_group_without_changing_stable_slug(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("group-edit", kwargs={"group_slug": self.group.slug}), {
            "name": "Renamed Group",
            "timezone": "America/Chicago",
        })
        self.assertRedirects(response, reverse("group-detail", kwargs={"group_slug": "original-group"}))
        self.group.refresh_from_db()
        self.assertEqual(self.group.name, "Renamed Group")
        self.assertEqual(self.group.timezone, "America/Chicago")
        self.assertEqual(self.group.slug, "original-group")

    def test_reader_cannot_edit_group(self):
        self.client.force_login(self.reader)
        response = self.client.get(reverse("group-edit", kwargs={"group_slug": self.group.slug}))
        self.assertEqual(response.status_code, 403)
        group_page = self.client.get(reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.assertNotContains(group_page, reverse("group-edit", kwargs={"group_slug": self.group.slug}))


class OwnerRemovalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("owner", password="test-password")
        self.reader = User.objects.create_user("delete-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Owner Group", slug="owner-group")
        Membership.objects.create(group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner")
        self.reader_membership = Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Reader")
        self.month = ChallengeMonth.objects.create(group=self.group, name="August 2026", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.OPEN)
        self.submission = BookSubmission.objects.create(month=self.month, participant=self.reader_membership, title="Removable Book", author="Author", book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 10), submitted_pages=200, approved_pages=200, status=BookSubmission.Status.APPROVED)

    def test_owner_soft_removes_book_from_totals(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("submission-remove", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.submission.pk}), {"reason": "Duplicate"})
        self.assertRedirects(response, self.month.get_absolute_url())
        self.submission.refresh_from_db()
        self.assertTrue(self.submission.is_removed)
        self.assertEqual(self.submission.approved_pages, 200)


class RegistrationAndGroupAccessTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_superuser(
            "registration-platform-owner",
            "platform-owner@example.com",
            "platform-owner-test-password-482!",
        )

    def test_registration_can_choose_built_in_avatar(self):
        response = self.client.post(reverse("register"), {
            "username": "avatar-signup",
            "email": "avatar@example.com",
            "password1": "long-registration-password-481!",
            "password2": "long-registration-password-481!",
            "selected_avatar": "3d_1.png",
        })
        self.assertRedirects(response, reverse("dashboard"))
        profile = UserProfile.objects.get(user__username="avatar-signup")
        self.assertEqual(profile.selected_avatar, "3d_1.png")

    def test_registered_user_creates_group_and_becomes_owner(self):
        response = self.client.post(reverse("register"), {
            "username": "new-reader",
            "email": "reader@example.com",
            "password1": "long-registration-password-481!",
            "password2": "long-registration-password-481!",
        })
        self.assertRedirects(response, reverse("dashboard"))
        response = self.client.post(reverse("group-create"), {
            "name": "Created Group",
            "timezone": "America/New_York",
            "access_code": "private-code-842",
        })
        group = ReadingGroup.objects.get(name="Created Group")
        self.assertRedirects(response, reverse("group-detail", kwargs={"group_slug": group.slug}))
        self.assertTrue(Membership.objects.filter(group=group, user__username="new-reader", role=Membership.Role.OWNER).exists())
        self.assertEqual(len(group.join_code), 6)
        self.assertTrue(group.join_code.isalnum())

    def test_access_code_joins_as_reader_then_owner_changes_role(self):
        owner = get_user_model().objects.create_user("code-owner", password="test-password")
        joiner = get_user_model().objects.create_user("code-joiner", password="test-password")
        self.client.force_login(owner)
        self.client.post(reverse("group-create"), {"name": "Code Group", "timezone": "America/New_York"})
        group = ReadingGroup.objects.get(name="Code Group")

        self.client.force_login(joiner)
        response = self.client.post(reverse("group-join"), {"access_code": group.join_code.lower()})
        self.assertRedirects(response, reverse("group-detail", kwargs={"group_slug": group.slug}))
        membership = Membership.objects.get(group=group, user=joiner)
        self.assertEqual(membership.role, Membership.Role.READER)

        self.client.force_login(owner)
        response = self.client.post(reverse("participant-role-edit", kwargs={"group_slug": group.slug, "pk": membership.pk}), {"role": Membership.Role.MODERATOR, "is_active": "on"})
        self.assertRedirects(response, reverse("participant-list", kwargs={"group_slug": group.slug}))
        membership.refresh_from_db()
        self.assertEqual(membership.role, Membership.Role.MODERATOR)

    def test_wrong_access_code_does_not_join(self):
        owner = get_user_model().objects.create_user("wrong-owner", password="test-password")
        joiner = get_user_model().objects.create_user("wrong-joiner", password="test-password")
        self.client.force_login(owner)
        self.client.post(reverse("group-create"), {"name": "Private Group", "timezone": "America/New_York"})
        group = ReadingGroup.objects.get(name="Private Group")
        self.client.force_login(joiner)
        response = self.client.post(reverse("group-join"), {"access_code": "BAD999"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Membership.objects.filter(group=group, user=joiner).exists())

    def test_owner_can_reveal_access_code_to_all_group_members(self):
        owner = get_user_model().objects.create_user("visible-code-owner", password="test-password")
        reader = get_user_model().objects.create_user("visible-code-reader", password="test-password")
        self.client.force_login(owner)
        self.client.post(reverse("group-create"), {"name": "Visible Code Group", "timezone": "America/New_York"})
        group = ReadingGroup.objects.get(name="Visible Code Group")
        code = group.join_code
        owner_page = self.client.get(reverse("group-detail", kwargs={"group_slug": group.slug}))
        self.assertNotContains(owner_page, code)
        access_code_page = self.client.get(reverse("group-access-code", kwargs={"group_slug": group.slug}))
        self.assertContains(access_code_page, code)

        self.client.force_login(reader)
        self.client.post(reverse("group-join"), {"access_code": code})
        reader_page = self.client.get(reverse("group-detail", kwargs={"group_slug": group.slug}))
        self.assertNotContains(reader_page, code)

        self.client.force_login(owner)
        response = self.client.post(reverse("group-access-code", kwargs={"group_slug": group.slug}), {
            "access_code_visibility": ReadingGroup.AccessCodeVisibility.MEMBERS,
        })
        self.assertRedirects(response, reverse("group-detail", kwargs={"group_slug": group.slug}))

        self.client.force_login(reader)
        reader_page = self.client.get(reverse("group-detail", kwargs={"group_slug": group.slug}))
        self.assertNotContains(reader_page, code)
        self.assertContains(reader_page, "Access Code")
        reader_access_page = self.client.get(reverse("group-access-code", kwargs={"group_slug": group.slug}))
        self.assertContains(reader_access_page, code)
        self.assertNotContains(reader_access_page, "Save Settings")

    def test_owner_can_regenerate_access_code(self):
        owner = get_user_model().objects.create_user("regenerate-owner", password="test-password")
        self.client.force_login(owner)
        self.client.post(reverse("group-create"), {"name": "Regenerate Group", "timezone": "America/New_York"})
        group = ReadingGroup.objects.get(name="Regenerate Group")
        old_code = group.join_code
        response = self.client.post(reverse("group-access-code", kwargs={"group_slug": group.slug}), {
            "access_code_visibility": ReadingGroup.AccessCodeVisibility.OWNER,
            "regenerate_code": "on",
        })
        self.assertRedirects(response, reverse("group-detail", kwargs={"group_slug": group.slug}))
        group.refresh_from_db()
        self.assertNotEqual(group.join_code, old_code)
        self.assertEqual(len(group.join_code), 6)


class TeamStatsVisibilityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("visibility-owner", password="test-password")
        self.mod = User.objects.create_user("visibility-mod", password="test-password")
        self.reader = User.objects.create_user("visibility-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Visibility Group", slug="visibility-group")
        Membership.objects.create(group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner")
        Membership.objects.create(group=self.group, user=self.mod, role=Membership.Role.MODERATOR, display_name="Mod")
        Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Reader")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Secret Month", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.OPEN)
        Team.objects.create(month=self.month, name="Team One", color="#6633cc")

    def month_page(self, user):
        self.client.force_login(user)
        return self.client.get(self.month.get_absolute_url())

    def test_owner_only_is_default_and_hides_comparison_from_reader_and_moderator(self):
        self.assertEqual(self.month.team_stats_visibility, ChallengeMonth.TeamStatsVisibility.OWNER)
        self.assertNotContains(self.month_page(self.reader), "Team Comparison")
        self.assertNotContains(self.month_page(self.mod), "Team Comparison")
        self.assertContains(self.month_page(self.owner), "Visibility")

    def test_owner_can_open_visibility_to_everyone(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("team-stats-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), {"team_stats_visibility": ChallengeMonth.TeamStatsVisibility.EVERYONE})
        self.assertRedirects(response, self.month.get_absolute_url())
        self.month.refresh_from_db()
        self.assertEqual(self.month.team_stats_visibility, ChallengeMonth.TeamStatsVisibility.EVERYONE)

    def test_staff_visibility_allows_moderator_but_not_reader(self):
        self.month.team_stats_visibility = ChallengeMonth.TeamStatsVisibility.STAFF
        self.month.save(update_fields=["team_stats_visibility"])
        self.client.force_login(self.reader)
        reader_teams = self.client.get(reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertContains(reader_teams, "Page totals hidden")
        self.client.force_login(self.mod)
        mod_teams = self.client.get(reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertNotContains(mod_teams, "Page totals hidden")


class MonthEnrollmentTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("enrollment-owner", password="test-password")
        self.admin = User.objects.create_user("enrollment-admin", password="test-password")
        self.reader = User.objects.create_user("enrollment-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Enrollment Group", slug="enrollment-group")
        Membership.objects.create(group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner")
        Membership.objects.create(group=self.group, user=self.admin, role=Membership.Role.ADMIN, display_name="Admin")
        self.reader_membership = Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Reader")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Enrollment Month", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.OPEN)
        self.team = Team.objects.create(month=self.month, name="Enrollment Team")

    def test_team_assignment_automatically_enrolls_participant(self):
        TeamAssignment.objects.create(month=self.month, team=self.team, participant=self.reader_membership)
        self.assertTrue(MonthEnrollment.objects.filter(month=self.month, participant=self.reader_membership).exists())

    def test_unenrolled_reader_cannot_submit(self):
        self.client.force_login(self.reader)
        response = self.client.post(reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), {
            "title": "Blocked Book", "author": "Author", "book_format": BookSubmission.Format.EBOOK,
            "completed_on": "2026-08-10", "submitted_pages": 200, "notes": "",
        })
        self.assertRedirects(response, self.month.get_absolute_url())
        self.assertFalse(BookSubmission.objects.exists())

    def test_admin_can_enroll_reader_without_team(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("month-participant-add", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), {"participant": self.reader_membership.pk})
        self.assertRedirects(response, reverse("month-participant-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertTrue(MonthEnrollment.objects.filter(month=self.month, participant=self.reader_membership).exists())
        self.assertFalse(TeamAssignment.objects.filter(month=self.month, participant=self.reader_membership).exists())

    def test_admin_can_optionally_assign_team_while_enrolling(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("month-participant-add", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), {
            "participant": self.reader_membership.pk,
            "team": self.team.pk,
        })
        self.assertRedirects(response, reverse("month-participant-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertTrue(MonthEnrollment.objects.filter(month=self.month, participant=self.reader_membership).exists())
        self.assertTrue(TeamAssignment.objects.filter(month=self.month, participant=self.reader_membership, team=self.team).exists())

    def test_admin_can_assign_move_and_unassign_enrolled_reader(self):
        enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader_membership)
        second_team = Team.objects.create(month=self.month, name="Second Team")
        self.client.force_login(self.admin)

        edit_url = reverse("month-participant-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": enrollment.pk})
        self.client.post(edit_url, {"team": self.team.pk})
        self.assertTrue(TeamAssignment.objects.filter(month=self.month, participant=self.reader_membership, team=self.team).exists())

        self.client.post(edit_url, {"team": second_team.pk})
        self.assertTrue(TeamAssignment.objects.filter(month=self.month, participant=self.reader_membership, team=second_team).exists())
        self.assertFalse(TeamAssignment.objects.filter(month=self.month, participant=self.reader_membership, team=self.team).exists())

        self.client.post(edit_url, {"team": ""})
        self.assertFalse(TeamAssignment.objects.filter(month=self.month, participant=self.reader_membership).exists())
        self.assertTrue(MonthEnrollment.objects.filter(pk=enrollment.pk).exists())

    def test_owner_removes_reader_from_month_but_preserves_submission(self):
        enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader_membership)
        TeamAssignment.objects.create(month=self.month, participant=self.reader_membership, team=self.team)
        submission = BookSubmission.objects.create(month=self.month, participant=self.reader_membership, title="Preserved Book", author="Author", book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 10), submitted_pages=200)
        self.client.force_login(self.owner)

        response = self.client.post(reverse("month-participant-remove", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": enrollment.pk}))
        self.assertRedirects(response, reverse("month-participant-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertFalse(MonthEnrollment.objects.filter(pk=enrollment.pk).exists())
        self.assertFalse(TeamAssignment.objects.filter(month=self.month, participant=self.reader_membership).exists())
        self.assertTrue(BookSubmission.objects.filter(pk=submission.pk).exists())


class MonthLifecycleTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("lifecycle-owner", password="test-password")
        self.admin = User.objects.create_user("lifecycle-admin", password="test-password")
        self.group = ReadingGroup.objects.create(name="Lifecycle Group", slug="lifecycle-group")
        Membership.objects.create(group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner")
        Membership.objects.create(group=self.group, user=self.admin, role=Membership.Role.ADMIN, display_name="Admin")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Draft Month", starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 30), status=ChallengeMonth.Status.DRAFT)

    def test_admin_can_change_draft_to_open(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("month-edit", kwargs={"group_slug": self.group.slug, "pk": self.month.pk}), {
            "name": "Draft Month",
            "starts_on": "2026-09-01",
            "ends_on": "2026-09-30",
            "late_entry_deadline": "",
            "status": ChallengeMonth.Status.OPEN,
        })
        self.assertRedirects(response, self.month.get_absolute_url())
        self.month.refresh_from_db()
        self.assertEqual(self.month.status, ChallengeMonth.Status.OPEN)

    def test_owner_can_delete_draft(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("month-delete", kwargs={"group_slug": self.group.slug, "pk": self.month.pk}))
        self.assertRedirects(response, reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.assertFalse(ChallengeMonth.objects.filter(pk=self.month.pk).exists())

    def test_delete_control_is_only_on_edit_page(self):
        self.client.force_login(self.owner)
        month_page = self.client.get(self.month.get_absolute_url())
        self.assertNotContains(month_page, "Delete Draft")
        edit_page = self.client.get(reverse("month-edit", kwargs={"group_slug": self.group.slug, "pk": self.month.pk}))
        self.assertContains(edit_page, "Delete Draft")

    def test_open_month_cannot_be_deleted(self):
        self.month.status = ChallengeMonth.Status.OPEN
        self.month.save(update_fields=["status"])
        self.client.force_login(self.owner)
        response = self.client.post(reverse("month-delete", kwargs={"group_slug": self.group.slug, "pk": self.month.pk}))
        self.assertRedirects(response, self.month.get_absolute_url())
        self.assertTrue(ChallengeMonth.objects.filter(pk=self.month.pk).exists())

    def test_archived_month_is_hidden_until_archive_is_selected(self):
        self.month.status = ChallengeMonth.Status.ARCHIVED
        self.month.save(update_fields=["status"])
        self.client.force_login(self.owner)

        group_page = self.client.get(reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.assertNotContains(group_page, self.month.name)

        month_list = self.client.get(reverse("month-list", kwargs={"group_slug": self.group.slug}))
        self.assertNotContains(month_list, self.month.name)
        self.assertContains(month_list, "View Archive (1)")

        archive = self.client.get(reverse("month-list", kwargs={"group_slug": self.group.slug}) + "?archive=1")
        self.assertContains(archive, self.month.name)
        self.assertContains(archive, "View Active Months")


class TeamManagementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("team-owner", password="test-password")
        self.admin = User.objects.create_user("team-admin", password="test-password")
        self.reader = User.objects.create_user("team-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Team Management Group", slug="team-management-group")
        Membership.objects.create(group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner")
        Membership.objects.create(group=self.group, user=self.admin, role=Membership.Role.ADMIN, display_name="Admin")
        self.reader_membership = Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Reader")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Team Draft", starts_on=date(2026, 11, 1), ends_on=date(2026, 11, 30), status=ChallengeMonth.Status.DRAFT)
        self.team = Team.objects.create(month=self.month, name="Original Team", color="#112233")

    def test_admin_can_rename_and_recolor_team(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("team-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.team.pk}), {
            "name": "Renamed Team",
            "color": "#abcdef",
        })
        self.assertRedirects(response, reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.team.refresh_from_db()
        self.assertEqual(self.team.name, "Renamed Team")
        self.assertEqual(self.team.color, "#abcdef")

    def test_archived_team_is_hidden_from_active_view_and_can_be_restored(self):
        self.client.force_login(self.admin)
        toggle_url = reverse("team-archive-toggle", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.team.pk})
        confirmation = self.client.get(toggle_url)
        self.assertContains(confirmation, f"Archive {self.team.name}?")
        self.assertContains(confirmation, "Confirm Archive")
        self.team.refresh_from_db()
        self.assertFalse(self.team.is_archived)
        response = self.client.post(toggle_url)
        self.assertRedirects(response, reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}) + "?archive=1")
        self.team.refresh_from_db()
        self.assertTrue(self.team.is_archived)
        active_page = self.client.get(reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertNotContains(active_page, self.team.name)
        archive_page = self.client.get(reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}) + "?archive=1")
        self.assertContains(archive_page, self.team.name)

        self.client.post(toggle_url)
        self.team.refresh_from_db()
        self.assertFalse(self.team.is_archived)

    def test_owner_can_delete_only_unused_draft_team(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("team-delete", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.team.pk}))
        self.assertRedirects(response, reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertFalse(Team.objects.filter(pk=self.team.pk).exists())

    def test_assigned_or_non_draft_team_is_protected_from_deletion(self):
        TeamAssignment.objects.create(month=self.month, team=self.team, participant=self.reader_membership)
        self.client.force_login(self.owner)
        delete_url = reverse("team-delete", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.team.pk})
        response = self.client.post(delete_url)
        self.assertRedirects(response, reverse("team-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.team.pk}))
        self.assertTrue(Team.objects.filter(pk=self.team.pk).exists())

        TeamAssignment.objects.filter(team=self.team).delete()
        self.month.status = ChallengeMonth.Status.OPEN
        self.month.save(update_fields=["status"])
        response = self.client.post(delete_url)
        self.assertRedirects(response, reverse("team-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.team.pk}))
        self.assertTrue(Team.objects.filter(pk=self.team.pk).exists())


class ReaderProfileTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.profile_user = User.objects.create_user("profile-reader", password="test-password")
        self.other_reader = User.objects.create_user("profile-other", password="test-password")
        self.moderator = User.objects.create_user("profile-moderator", password="test-password")
        self.group = ReadingGroup.objects.create(name="Profile Group", slug="profile-group")
        self.profile_membership = Membership.objects.create(group=self.group, user=self.profile_user, role=Membership.Role.READER, display_name="Profile Reader")
        Membership.objects.create(group=self.group, user=self.other_reader, role=Membership.Role.READER, display_name="Other Reader")
        Membership.objects.create(group=self.group, user=self.moderator, role=Membership.Role.MODERATOR, display_name="Moderator")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Profile Month", starts_on=date(2026, 12, 1), ends_on=date(2026, 12, 31), status=ChallengeMonth.Status.OPEN)
        self.team = Team.objects.create(month=self.month, name="Profile Team", color="#445566")
        TeamAssignment.objects.create(month=self.month, team=self.team, participant=self.profile_membership)
        BookSubmission.objects.create(month=self.month, participant=self.profile_membership, title="Private Profile Book", author="Author", book_format=BookSubmission.Format.HARDCOVER, completed_on=date(2026, 12, 5), submitted_pages=321, approved_pages=300, status=BookSubmission.Status.APPROVED)
        BookSubmission.objects.create(month=self.month, participant=self.profile_membership, title="Pending Profile Book", author="Author", book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 12, 6), submitted_pages=200, status=BookSubmission.Status.PENDING)
        BookSubmission.objects.create(month=self.month, participant=self.profile_membership, title="Rejected Profile Book", author="Author", book_format=BookSubmission.Format.PAPERBACK, completed_on=date(2026, 12, 7), submitted_pages=150, status=BookSubmission.Status.REJECTED)
        self.url = reverse("participant-detail", kwargs={"group_slug": self.group.slug, "pk": self.profile_membership.pk})

    def test_reader_sees_own_detailed_monthly_profile_without_book_titles(self):
        self.client.force_login(self.profile_user)
        response = self.client.get(self.url)
        self.assertContains(response, "Monthly History")
        self.assertContains(response, "Profile Month")
        self.assertContains(response, "Profile Team")
        self.assertContains(response, "300")
        self.assertEqual(response.context["approved_pages"], 300)
        self.assertEqual(response.context["months"][0].participant_pages, 300)
        self.assertEqual(response.context["months"][0].participant_books, 1)
        self.assertNotContains(response, "Private Profile Book")

    def test_other_reader_sees_summary_but_not_detailed_history(self):
        self.client.force_login(self.other_reader)
        response = self.client.get(self.url)
        self.assertContains(response, "Verified Pages")
        self.assertContains(response, "Approved Books")
        self.assertNotContains(response, "Monthly History")
        self.assertNotContains(response, "Profile Team")
        self.assertNotContains(response, "Private Profile Book")

    def test_moderator_sees_detailed_monthly_profile(self):
        self.client.force_login(self.moderator)
        response = self.client.get(self.url)
        self.assertContains(response, "Monthly History")
        self.assertContains(response, "Profile Month")
        self.assertContains(response, "Profile Team")


class AnnouncementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("announcement-owner", password="test-password")
        self.moderator = User.objects.create_user("announcement-moderator", password="test-password")
        self.reader = User.objects.create_user("announcement-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Announcement Group", slug="announcement-group", announcement_enabled=True, announcement="Group-wide news")
        Membership.objects.create(group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner")
        Membership.objects.create(group=self.group, user=self.moderator, role=Membership.Role.MODERATOR, display_name="Moderator")
        Membership.objects.create(group=self.group, user=self.reader, role=Membership.Role.READER, display_name="Reader")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Announcement Month",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.OPEN,
        )
        self.client.force_login(self.owner)

    def test_group_announcement_appears_above_group_summary(self):
        response = self.client.get(reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.assertContains(response, "Group-wide news")
        self.assertLess(response.content.index(b"Group-wide news"), response.content.index(b"Participants"))

    def test_month_can_inherit_or_hide_group_announcement(self):
        inherited = self.client.get(self.month.get_absolute_url())
        self.assertContains(inherited, "Group-wide news")
        self.month.announcement_mode = ChallengeMonth.AnnouncementMode.NONE
        self.month.save(update_fields=["announcement_mode"])
        hidden = self.client.get(self.month.get_absolute_url())
        self.assertNotContains(hidden, "Group-wide news")

    def test_month_custom_announcement_overrides_group_announcement(self):
        self.month.announcement_mode = ChallengeMonth.AnnouncementMode.CUSTOM
        self.month.announcement = "Month-specific news"
        self.month.full_clean()
        self.month.save()
        response = self.client.get(self.month.get_absolute_url())
        self.assertContains(response, "Month-specific news")
        self.assertNotContains(response, "Group-wide news")

    def test_moderator_can_inline_edit_announcements(self):
        self.client.force_login(self.moderator)
        group_response = self.client.post(reverse("group-announcement-update", kwargs={"group_slug": self.group.slug}), {"announcement": "Updated group news"})
        self.assertRedirects(group_response, reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.group.refresh_from_db()
        self.assertEqual(self.group.announcement, "Updated group news")

        self.month.announcement_mode = ChallengeMonth.AnnouncementMode.CUSTOM
        self.month.announcement = "Old month news"
        self.month.save(update_fields=["announcement_mode", "announcement"])
        month_response = self.client.post(reverse("month-announcement-update", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), {"announcement": "Updated month news"})
        self.assertRedirects(month_response, self.month.get_absolute_url())
        self.month.refresh_from_db()
        self.assertEqual(self.month.announcement, "Updated month news")

    def test_reader_cannot_inline_edit_announcements(self):
        self.client.force_login(self.reader)
        response = self.client.post(reverse("group-announcement-update", kwargs={"group_slug": self.group.slug}), {"announcement": "Unauthorized"})
        self.assertEqual(response.status_code, 403)
        self.group.refresh_from_db()
        self.assertEqual(self.group.announcement, "Group-wide news")
