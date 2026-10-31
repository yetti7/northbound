from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import AuditEvent, ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, ReadingGroup, UserProfile


class DiscordUsernameTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.platform_owner = User.objects.create_superuser(
            "platform-owner", "platform@example.com", "test-password"
        )
        self.group_owner = User.objects.create_user("group-owner", password="test-password")
        self.host = User.objects.create_user("challenge-host", password="test-password")
        self.reader = User.objects.create_user(
            "discord-reader", "reader@example.com", "test-password"
        )
        self.other_reader = User.objects.create_user("other-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Discord Group", slug="discord-group")
        self.owner_membership = Membership.objects.create(
            group=self.group, user=self.group_owner, role=Membership.Role.OWNER, display_name="Group Owner"
        )
        self.host_membership = Membership.objects.create(
            group=self.group, user=self.host, role=Membership.Role.MEMBER, display_name="Challenge Host"
        )
        self.reader_membership = Membership.objects.create(
            group=self.group, user=self.reader, role=Membership.Role.MEMBER, display_name="Discord Reader"
        )
        self.other_membership = Membership.objects.create(
            group=self.group, user=self.other_reader, role=Membership.Role.MEMBER, display_name="Other Reader"
        )
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Discord Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.host_membership,
            role=ChallengeStaffAssignment.Role.HOST,
            assigned_by=self.group_owner,
        )
        MonthEnrollment.objects.create(month=self.month, participant=self.reader_membership)
        UserProfile.objects.create(user=self.reader, discord_username="northbound.reader")

    def account_payload(self, **overrides):
        payload = {
            "username": self.reader.username,
            "first_name": self.reader.first_name,
            "last_name": self.reader.last_name,
            "email": self.reader.email,
            "discord_username": "updated.reader",
        }
        payload.update(overrides)
        return payload

    def test_user_can_update_and_clear_own_optional_discord_username(self):
        self.client.force_login(self.reader)
        response = self.client.post(reverse("account"), self.account_payload())
        self.assertRedirects(response, reverse("account"))
        self.reader.northbound_profile.refresh_from_db()
        self.assertEqual(self.reader.northbound_profile.discord_username, "updated.reader")

        response = self.client.post(reverse("account"), self.account_payload(discord_username=""))
        self.assertRedirects(response, reverse("account"))
        self.reader.northbound_profile.refresh_from_db()
        self.assertEqual(self.reader.northbound_profile.discord_username, "")

    def test_visibility_preference_defaults_private_and_user_can_change_it(self):
        self.assertFalse(self.reader.northbound_profile.discord_username_is_public)
        self.client.force_login(self.reader)
        profile_url = reverse(
            "participant-detail",
            kwargs={"group_slug": self.group.slug, "pk": self.reader_membership.pk},
        )
        self.assertContains(self.client.get(profile_url), "northbound.reader")
        response = self.client.post(
            reverse("account"),
            self.account_payload(discord_username_is_public="on"),
        )
        self.assertRedirects(response, reverse("account"))
        self.reader.northbound_profile.refresh_from_db()
        self.assertTrue(self.reader.northbound_profile.discord_username_is_public)

        response = self.client.post(reverse("account"), self.account_payload())
        self.assertRedirects(response, reverse("account"))
        self.reader.northbound_profile.refresh_from_db()
        self.assertFalse(self.reader.northbound_profile.discord_username_is_public)

    def test_platform_owner_can_correct_discord_username_in_place_and_audit_field_only(self):
        original_user_id = self.reader.pk
        original_membership_id = self.reader_membership.pk
        self.client.force_login(self.platform_owner)
        edit_url = reverse("config-user-edit", kwargs={"pk": self.reader.pk})
        self.assertNotIn(
            "discord_username_is_public",
            self.client.get(edit_url).context["form"].fields,
        )
        response = self.client.post(
            edit_url,
            self.account_payload(discord_username="corrected.reader"),
        )
        self.assertRedirects(response, reverse("config-user-detail", kwargs={"pk": original_user_id}))
        self.reader.refresh_from_db()
        self.reader.northbound_profile.refresh_from_db()
        self.assertEqual(self.reader.pk, original_user_id)
        self.assertEqual(self.reader_membership.pk, original_membership_id)
        self.assertEqual(self.reader.northbound_profile.discord_username, "corrected.reader")
        audit = AuditEvent.objects.get(action="account.identity_updated", object_id=str(original_user_id))
        self.assertIn("Discord username", audit.summary)
        self.assertNotIn("corrected.reader", audit.summary)

    def test_platform_owner_cannot_change_visibility_preference(self):
        profile = self.reader.northbound_profile
        self.assertFalse(profile.discord_username_is_public)
        self.client.force_login(self.platform_owner)
        response = self.client.post(
            reverse("config-user-edit", kwargs={"pk": self.reader.pk}),
            self.account_payload(
                discord_username="corrected.reader",
                discord_username_is_public="on",
            ),
        )
        self.assertRedirects(response, reverse("config-user-detail", kwargs={"pk": self.reader.pk}))
        profile.refresh_from_db()
        self.assertFalse(profile.discord_username_is_public)

    def test_group_staff_can_view_profile_value_but_ordinary_member_cannot(self):
        url = reverse(
            "participant-detail",
            kwargs={"group_slug": self.group.slug, "pk": self.reader_membership.pk},
        )
        self.client.force_login(self.group_owner)
        self.assertContains(self.client.get(url), "northbound.reader")

        self.client.force_login(self.other_reader)
        self.assertNotContains(self.client.get(url), "northbound.reader")

        profile = self.reader.northbound_profile
        profile.discord_username_is_public = True
        profile.save(update_fields=["discord_username_is_public"])
        self.assertContains(self.client.get(url), "northbound.reader")

    def test_host_and_platform_owner_can_view_challenge_roster_value_but_ordinary_member_cannot(self):
        url = reverse(
            "month-participant-list",
            kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk},
        )
        for viewer in (self.host, self.platform_owner):
            with self.subTest(viewer=viewer.username):
                self.client.force_login(viewer)
                self.assertContains(self.client.get(url), "northbound.reader")

        self.client.force_login(self.other_reader)
        self.assertNotContains(self.client.get(url), "northbound.reader")

    def test_group_and_challenge_staff_have_no_account_edit_authority(self):
        edit_url = reverse("config-user-edit", kwargs={"pk": self.reader.pk})
        for viewer in (self.group_owner, self.host):
            with self.subTest(viewer=viewer.username):
                self.client.force_login(viewer)
                self.assertEqual(self.client.get(edit_url).status_code, 403)
