from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    BotmBook, ChallengeMonth, ChallengeStaffAssignment, Membership,
    MonthEnrollment, ReadingGroup, Team,
)


class GroupChallengeUi2Tests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user("ui2-owner")
        self.moderator_user = User.objects.create_user("ui2-moderator")
        self.reader_user = User.objects.create_user("ui2-reader")
        self.host_user = User.objects.create_user("ui2-host")
        self.floater_user = User.objects.create_user("ui2-floater")
        self.group = ReadingGroup.objects.create(name="UI Two Group", slug="ui-two-group")
        self.owner = Membership.objects.create(
            group=self.group, user=self.owner_user, display_name="Owner", role=Membership.Role.OWNER,
        )
        self.moderator = Membership.objects.create(
            group=self.group, user=self.moderator_user, display_name="Moderator", role=Membership.Role.MODERATOR,
            permission_overrides={"manage_group_settings": True, "manage_months": True},
        )
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Reader")
        self.host = Membership.objects.create(group=self.group, user=self.host_user, display_name="Host")
        self.floater = Membership.objects.create(group=self.group, user=self.floater_user, display_name="Floater")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="UI Two Challenge", starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30), status=ChallengeMonth.Status.ACTIVE,
            botm_enabled=True, games_enabled=False, tbr_enabled=False,
        )
        MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST,
            host_assignment_notice_seen_at=timezone.now(),
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER,
        )
        Team.objects.create(month=self.month, name="North")
        BotmBook.objects.create(
            month=self.month, position=1, title_snapshot="Catalog or Manual",
            author_snapshot="Author", page_count_snapshot=300,
        )
        self.group_url = reverse("group-detail", kwargs={"group_slug": self.group.slug})
        self.challenge_url = self.month.get_absolute_url()
        self.settings_url = reverse("challenge-settings", kwargs={
            "group_slug": self.group.slug, "month_pk": self.month.pk,
        })

    def get_as(self, user, url):
        self.client.force_login(user)
        return self.client.get(url)

    def test_group_reader_navigation_and_manager_actions_keep_existing_destinations(self):
        reader = self.get_as(self.reader_user, self.group_url)
        self.assertEqual(reader.status_code, 200)
        self.assertContains(reader, "Group Navigation")
        self.assertContains(reader, reverse("participant-list", kwargs={"group_slug": self.group.slug}))
        self.assertContains(reader, reverse("month-list", kwargs={"group_slug": self.group.slug}))
        self.assertNotContains(reader, "Group Settings")
        self.assertNotContains(reader, "Create Challenge")
        self.assertNotContains(reader, "Access Code")
        self.assertNotContains(reader, "Group Management")
        self.assertNotContains(reader, ">Add Member</a>")
        for manager in (self.owner_user, self.moderator_user):
            with self.subTest(manager=manager.username):
                response = self.get_as(manager, self.group_url)
                self.assertContains(response, f'href="{reverse("group-edit", kwargs={"group_slug": self.group.slug})}">Group Settings</a>')
                self.assertContains(response, f'href="{reverse("month-create", kwargs={"group_slug": self.group.slug})}">Create Challenge</a>')
                self.assertContains(response, f'href="{reverse("group-access-code", kwargs={"group_slug": self.group.slug})}">Access Code</a>')
                self.assertNotContains(response, "Group Management")
                self.assertNotContains(response, ">Add Member</a>")
                self.assertNotContains(response, ">Edit</a>")
                self.assertEqual(response.content.decode().count(">Create Challenge</a>"), 1)
        members = self.get_as(
            self.owner_user,
            reverse("participant-list", kwargs={"group_slug": self.group.slug}),
        )
        self.assertContains(members, ">Add Member</a>")

    def test_group_cards_use_compact_shared_spacing_and_challenge_list_reuses_lifecycle_badges(self):
        upcoming = ChallengeMonth.objects.create(
            group=self.group, name="Upcoming UI Two Challenge", starts_on=date(2026, 10, 1),
            ends_on=date(2026, 10, 31), status=ChallengeMonth.Status.UPCOMING,
        )
        completed = ChallengeMonth.objects.create(
            group=self.group, name="Completed UI Two Challenge", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.COMPLETED,
        )
        group_response = self.get_as(self.reader_user, self.group_url)
        self.assertContains(group_response, "group-navigation-grid group-detail-card-grid")
        self.assertContains(group_response, 'class="card-grid group-detail-card-grid"')

        response = self.get_as(
            self.reader_user,
            reverse("month-list", kwargs={"group_slug": self.group.slug}),
        )
        self.assertContains(
            response,
            '<span class="pill status-badge status-success">Active</span>',
            html=True,
        )
        self.assertContains(
            response,
            '<span class="pill status-badge status-info">Upcoming</span>',
            html=True,
        )
        self.assertContains(
            response,
            '<span class="pill status-badge status-neutral">Completed</span>',
            html=True,
        )
        self.assertContains(response, upcoming.name)
        self.assertContains(response, completed.name)

        css = (Path(__file__).resolve().parent.parent / "static/css/theme.css").read_text()
        self.assertIn(".group-detail-card-grid .navigation-card{padding:20px}", css)
        self.assertIn(".group-detail-card-grid .navigation-card::after{padding-top:12px}", css)

    def test_challenge_header_permissions_settings_and_protected_layout(self):
        reader = self.get_as(self.reader_user, self.challenge_url)
        self.assertEqual(reader.status_code, 200)
        self.assertContains(reader, "page-heading page-header")
        self.assertContains(reader, "challenge-upper-layout")
        self.assertContains(reader, "challenge-schedule")
        self.assertContains(reader, "challenge-botm-preview")
        self.assertContains(reader, ">Submit Book</a>")
        self.assertNotContains(reader, "Challenge Settings")
        self.assertNotContains(reader, "Configure in Challenge Settings")
        host = self.get_as(self.host_user, self.challenge_url)
        self.assertContains(host, f'href="{self.settings_url}">Challenge Settings</a>')
        self.assertContains(host, "Challenge Staff")
        self.assertContains(host, "Hosts")
        self.assertContains(host, "Floaters")
        self.assertEqual(host.content.decode().count(f'href="{self.settings_url}"'), 1)
        self.assertNotContains(host, "Back to Month")

    def test_teams_duplicate_is_removed_but_team_navigation_and_comparison_remain_distinct(self):
        response = self.get_as(self.host_user, self.challenge_url)
        team_url = reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.assertContains(
            response,
            f'<a class="stat stat-link" href="{team_url}"><span>Teams</span>',
        )
        self.assertContains(response, "Team Comparison")
        self.assertContains(
            response,
            f'<a class="bar-chart team-comparison-link" href="{team_url}" aria-label="View Teams for {self.month.name}">',
        )
        self.assertContains(response, "View Teams →")
        self.assertContains(response, "North")
        self.assertContains(response, "<strong>0</strong>", html=True)
        self.assertNotContains(response, "team-card")

    def test_settings_landing_uses_grouped_navigation_without_changing_routes(self):
        response = self.get_as(self.host_user, self.settings_url)
        self.assertEqual(response.status_code, 200)
        for heading in ("Core Configuration", "People &amp; Operations", "Optional Features", "Visibility &amp; Lifecycle"):
            self.assertContains(response, heading)
        for name in (
            "challenge-general-settings", "challenge-schedule-settings", "challenge-signup-settings",
            "challenge-progress-checkpoints", "challenge-floater-list", "team-list", "theme-list",
            "challenge-games-settings", "challenge-botm-settings", "botm-list",
        ):
            self.assertContains(response, reverse(name, kwargs={
                "group_slug": self.group.slug, "month_pk": self.month.pk,
            }))

    def test_needs_attention_is_one_stable_authorized_destination_at_zero(self):
        destination = reverse("needs-attention")
        for user in (self.host_user, self.floater_user):
            with self.subTest(user=user.username):
                response = self.get_as(user, self.challenge_url)
                self.assertEqual(response.content.decode().count(f'href="{destination}"'), 1)
                self.assertContains(response, "Needs Attention")
                self.assertNotContains(response, "needs-attention-count")
        reader = self.get_as(self.reader_user, self.challenge_url)
        self.assertNotContains(reader, destination)

    def test_ui2_mobile_rules_preserve_stacking_and_overflow_safety(self):
        with open("static/css/theme.css", encoding="utf-8") as stylesheet:
            css = stylesheet.read()
        self.assertIn("@media(max-width:800px){.challenge-upper-layout", css)
        self.assertIn(".group-navigation-grid,.challenge-staff-grid{grid-template-columns:1fr}", css)
        self.assertIn(".group-management-actions{align-items:flex-start;flex-direction:column}", css)
