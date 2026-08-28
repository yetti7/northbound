from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, ReadingGroup, Team, TeamAssignment
from .permissions import can_manage_challenge_announcements, can_manage_group_announcements


class AnnouncementAuthoritySeparationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.users = {
            key: (User.objects.create_superuser("announcement-platform") if key == "platform" else User.objects.create_user(f"announcement-{key}"))
            for key in ("owner", "moderator", "host", "floater", "leader", "reader", "platform")
        }
        self.group = ReadingGroup.objects.create(
            name="Announcement Authority",
            slug="announcement-authority",
            announcement_enabled=True,
            announcement="Existing Group announcement",
        )
        self.members = {
            "owner": self.member("owner", Membership.Role.OWNER),
            "moderator": self.member("moderator", Membership.Role.MODERATOR, {"manage_months": True}),
            "host": self.member("host"),
            "floater": self.member("floater"),
            "leader": self.member("leader"),
            "reader": self.member("reader"),
        }
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Announcement Challenge",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            status=ChallengeMonth.Status.ACTIVE,
            announcement_mode=ChallengeMonth.AnnouncementMode.CUSTOM,
            announcement="Existing Challenge announcement",
        )
        self.team = Team.objects.create(month=self.month, name="Announcement Team")
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.members["host"], role=ChallengeStaffAssignment.Role.HOST)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.members["floater"], role=ChallengeStaffAssignment.Role.FLOATER)
        for key in ("leader", "reader"):
            MonthEnrollment.objects.create(month=self.month, participant=self.members[key])
            TeamAssignment.objects.create(month=self.month, team=self.team, participant=self.members[key])
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            team=self.team,
            membership=self.members["leader"],
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        )
        self.group_url = reverse("group-announcement-update", kwargs={"group_slug": self.group.slug})
        self.challenge_url = reverse("month-announcement-update", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.settings_url = reverse("challenge-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})

    def member(self, key, role=Membership.Role.MEMBER, overrides=None):
        return Membership.objects.create(
            group=self.group,
            user=self.users[key],
            role=role,
            display_name=key.title(),
            permission_overrides=overrides or {},
        )

    def test_group_and_challenge_helpers_are_distinct(self):
        self.assertTrue(can_manage_group_announcements(self.users["owner"], self.group))
        self.assertTrue(can_manage_group_announcements(self.users["moderator"], self.group))
        self.assertFalse(can_manage_group_announcements(self.users["host"], self.group))
        self.assertTrue(can_manage_challenge_announcements(self.users["owner"], self.month))
        self.assertTrue(can_manage_challenge_announcements(self.users["moderator"], self.month))
        self.assertTrue(can_manage_challenge_announcements(self.users["host"], self.month))
        for key in ("floater", "leader", "reader"):
            self.assertFalse(can_manage_challenge_announcements(self.users[key], self.month))
        self.assertTrue(can_manage_group_announcements(self.users["platform"], self.group))
        self.assertTrue(can_manage_challenge_announcements(self.users["platform"], self.month))

    def test_group_announcement_uses_only_group_authority(self):
        for key in ("owner", "moderator", "platform"):
            self.client.force_login(self.users[key])
            challenge_before = self.month.announcement
            response = self.client.post(self.group_url, {"announcement": f"Group update by {key}"})
            self.assertRedirects(response, reverse("group-detail", kwargs={"group_slug": self.group.slug}))
            self.month.refresh_from_db()
            self.assertEqual(self.month.announcement, challenge_before)
        for key in ("host", "floater", "leader", "reader"):
            self.client.force_login(self.users[key])
            self.assertEqual(self.client.post(self.group_url, {"announcement": "Unauthorized Group update"}).status_code, 403)
        self.group.refresh_from_db()
        self.assertNotEqual(self.group.announcement, "Unauthorized Group update")

    def test_challenge_announcement_uses_only_challenge_operational_or_admin_authority(self):
        for key in ("owner", "moderator", "host", "platform"):
            self.client.force_login(self.users[key])
            group_before = self.group.announcement
            response = self.client.post(self.challenge_url, {"announcement": f"Challenge update by {key}"})
            self.assertRedirects(response, self.settings_url)
            self.group.refresh_from_db()
            self.assertEqual(self.group.announcement, group_before)
        for key in ("floater", "leader", "reader"):
            self.client.force_login(self.users[key])
            self.assertEqual(self.client.post(self.challenge_url, {"announcement": "Unauthorized Challenge update"}).status_code, 403)
        self.month.refresh_from_db()
        self.assertNotEqual(self.month.announcement, "Unauthorized Challenge update")

    def test_ui_uses_unambiguous_announcement_language(self):
        self.client.force_login(self.users["owner"])
        group_page = self.client.get(reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.assertContains(group_page, "Edit Group Announcement")
        self.assertContains(group_page, '<label for="group-announcement">Group Announcement</label>')
        challenge_page = self.client.get(self.month.get_absolute_url())
        self.assertContains(challenge_page, "Group Announcement")
        self.assertContains(challenge_page, "Existing Group announcement")
        self.assertContains(challenge_page, "Challenge Announcement")
        self.assertContains(challenge_page, "Existing Challenge announcement")
        self.assertNotContains(challenge_page, "Inherited")
        self.assertNotContains(challenge_page, "Edit Group Announcement")
        self.assertNotContains(challenge_page, "Edit Challenge Announcement")

        settings_page = self.client.get(self.settings_url)
        self.assertContains(settings_page, "Edit Challenge Announcement")
        editor = self.client.get(self.challenge_url)
        self.assertContains(editor, "<h1>Challenge Announcement</h1>", html=True)
        self.assertContains(editor, "Save Challenge Announcement")
        self.assertNotContains(editor, "Group Announcement")
        self.assertNotContains(editor, "Inherited")
        self.assertEqual(editor.context["logical_parent_url"], self.settings_url)

    def test_group_announcement_remains_separate_when_challenge_has_no_message(self):
        self.month.announcement_mode = ChallengeMonth.AnnouncementMode.INHERIT
        self.month.announcement = "Stored but inactive Challenge text"
        self.month.save(update_fields=["announcement_mode", "announcement"])
        self.client.force_login(self.users["host"])

        detail = self.client.get(self.month.get_absolute_url())
        self.assertContains(detail, "Group Announcement")
        self.assertContains(detail, "Existing Group announcement")
        self.assertNotContains(detail, "Stored but inactive Challenge text")
        self.assertNotContains(detail, "Inherited")

    def test_blank_challenge_announcement_disables_only_challenge_message(self):
        self.client.force_login(self.users["host"])
        response = self.client.post(self.challenge_url, {"announcement": ""})
        self.assertRedirects(response, self.settings_url)
        self.month.refresh_from_db()
        self.group.refresh_from_db()
        self.assertEqual(self.month.announcement, "")
        self.assertEqual(self.month.announcement_mode, ChallengeMonth.AnnouncementMode.NONE)
        self.assertEqual(self.group.announcement, "Existing Group announcement")
