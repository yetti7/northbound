from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, ReadingGroup, Team, TeamAssignment


class LogicalNavigationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user("navigation-owner", password="test-password")
        self.host_user = User.objects.create_user("navigation-host", password="test-password")
        self.reader_user = User.objects.create_user("navigation-reader", password="test-password")
        self.floater_user = User.objects.create_user("navigation-floater", password="test-password")
        self.group = ReadingGroup.objects.create(name="Navigation Group", slug="navigation-group")
        self.owner = Membership.objects.create(
            group=self.group,
            user=self.owner_user,
            role=Membership.Role.OWNER,
            display_name="Owner",
        )
        self.host = Membership.objects.create(
            group=self.group,
            user=self.host_user,
            role=Membership.Role.MEMBER,
            display_name="Host",
        )
        self.reader = Membership.objects.create(
            group=self.group,
            user=self.reader_user,
            role=Membership.Role.MEMBER,
            display_name="Reader",
        )
        self.floater = Membership.objects.create(
            group=self.group,
            user=self.floater_user,
            role=Membership.Role.MEMBER,
            display_name="Floater",
        )
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Navigation Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
        )
        self.host_assignment = ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.host,
            role=ChallengeStaffAssignment.Role.HOST,
            assigned_by=self.owner_user,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.owner,
            role=ChallengeStaffAssignment.Role.HOST,
            assigned_by=self.owner_user,
        )
        self.floater_assignment = ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.floater,
            role=ChallengeStaffAssignment.Role.FLOATER,
            assigned_by=self.owner_user,
        )
        self.team = Team.objects.create(month=self.month, name="Navigation Team")
        MonthEnrollment.objects.create(month=self.month, participant=self.reader, enrolled_by=self.owner_user)
        TeamAssignment.objects.create(month=self.month, participant=self.reader, team=self.team)
        self.leader_assignment = ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.reader,
            team=self.team,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
            assigned_by=self.owner_user,
        )
        self.client.force_login(self.owner_user)

    def assert_logical_back(self, url, expected_url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["logical_parent_url"], expected_url)
        self.assertContains(response, f'href="{expected_url}"')
        self.assertNotContains(response, "data-back-button")

    def test_challenge_management_pages_return_to_challenge_dashboard(self):
        challenge_url = self.month.get_absolute_url()
        for view_name, extra_kwargs in (
            ("challenge-host-list", {}),
            ("challenge-floater-list", {}),
            ("team-list", {}),
            ("month-participant-list", {}),
        ):
            with self.subTest(view_name=view_name):
                self.assert_logical_back(
                    reverse(
                        view_name,
                        kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, **extra_kwargs},
                    ),
                    challenge_url,
                )

    def test_staffing_confirmations_return_to_their_management_pages(self):
        cases = (
            (
                "challenge-host-end",
                {"pk": self.host_assignment.pk},
                "challenge-host-list",
                {},
            ),
            (
                "challenge-floater-end",
                {"pk": self.floater_assignment.pk},
                "challenge-floater-list",
                {},
            ),
            (
                "team-leader-end",
                {"team_pk": self.team.pk, "pk": self.leader_assignment.pk},
                "team-leader-list",
                {"team_pk": self.team.pk},
            ),
        )
        for view_name, view_kwargs, parent_name, parent_kwargs in cases:
            with self.subTest(view_name=view_name):
                kwargs = {"group_slug": self.group.slug, "month_pk": self.month.pk, **view_kwargs}
                parent_url = reverse(
                    parent_name,
                    kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, **parent_kwargs},
                )
                response = self.client.get(reverse(view_name, kwargs=kwargs))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["logical_parent_url"], parent_url)
                self.assertContains(response, f'href="{parent_url}">Cancel</a>')

    def test_confirm_host_removal_then_management_back_returns_to_challenge(self):
        host_list_url = reverse(
            "challenge-host-list",
            kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk},
        )
        remove_url = reverse(
            "challenge-host-end",
            kwargs={
                "group_slug": self.group.slug,
                "month_pk": self.month.pk,
                "pk": self.host_assignment.pk,
            },
        )
        confirmation = self.client.get(remove_url)
        self.assertContains(confirmation, f'href="{host_list_url}">Cancel</a>')

        response = self.client.post(remove_url)
        self.assertRedirects(response, host_list_url)

        management = self.client.get(host_list_url)
        self.assertEqual(management.context["logical_parent_url"], self.month.get_absolute_url())
        self.assertContains(management, f'href="{self.month.get_absolute_url()}">← Back</a>')

    def test_team_leader_management_returns_to_teams(self):
        team_list_url = reverse(
            "team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}
        )
        leader_list_url = reverse(
            "team-leader-list",
            kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "team_pk": self.team.pk},
        )
        self.assert_logical_back(leader_list_url, team_list_url)

    def test_team_detail_returns_to_teams(self):
        team_list_url = reverse(
            "team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}
        )
        team_detail_url = reverse(
            "team-detail",
            kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.team.pk},
        )
        self.assert_logical_back(team_detail_url, team_list_url)
