from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, ReadingGroup, Team, TeamAssignment, UserProfile
from .reader_planning import historical_reader_planning_data


class TeamPlanningFoundationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user("hierarchy-owner")
        self.host_user = User.objects.create_user("hierarchy-host")
        self.floater_user = User.objects.create_user("hierarchy-floater")
        self.reader_user = User.objects.create_user("hierarchy-reader")
        self.platform_owner = User.objects.create_superuser("hierarchy-platform")
        self.group = ReadingGroup.objects.create(name="Hierarchy Group", slug="hierarchy-group")
        self.owner = self.member(self.owner_user, "Owner", Membership.Role.OWNER)
        self.host = self.member(self.host_user, "Host")
        self.floater = self.member(self.floater_user, "Floater")
        self.reader = self.member(self.reader_user, "Aaron Reader")
        self.leader_z_user = User.objects.create_user("hierarchy-leader-z")
        self.leader_a_user = User.objects.create_user("hierarchy-leader-a")
        self.reader_b_user = User.objects.create_user("hierarchy-reader-b")
        self.leader_z = self.member(self.leader_z_user, "Zoe Leader")
        self.leader_a = self.member(self.leader_a_user, "Amy Leader")
        self.reader_b = self.member(self.reader_b_user, "Beth Reader")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Hierarchy Challenge",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            status=ChallengeMonth.Status.UPCOMING,
            team_standings_visibility=ChallengeMonth.CompetitionVisibility.EVERYBODY,
            reader_scores_visibility=ChallengeMonth.CompetitionVisibility.EVERYBODY,
        )
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER)
        self.team = Team.objects.create(month=self.month, name="Purple Team", color="#7654aa")
        for participant in (self.reader, self.leader_z, self.leader_a, self.reader_b):
            MonthEnrollment.objects.create(month=self.month, participant=participant)
            TeamAssignment.objects.create(month=self.month, participant=participant, team=self.team)
        for leader in (self.leader_z, self.leader_a):
            ChallengeStaffAssignment.objects.create(
                month=self.month,
                membership=leader,
                team=self.team,
                role=ChallengeStaffAssignment.Role.TEAM_LEADER,
            )
        UserProfile.objects.create(user=self.reader.user, discord_username="private-reader")
        UserProfile.objects.create(user=self.leader_a.user, discord_username="private-leader")
        self.book(self.reader, "Reader Book", 100, 20)
        self.book(self.leader_a, "Leader Book", 200, 0)
        self.book(self.reader_b, "Beth Book", 50, 60)
        history = ChallengeMonth.objects.create(
            group=self.group,
            name="Historical Challenge",
            starts_on=date(2026, 7, 1),
            ends_on=date(2026, 7, 31),
            status=ChallengeMonth.Status.COMPLETED,
        )
        MonthEnrollment.objects.create(month=history, participant=self.reader)
        MonthEnrollment.objects.create(month=history, participant=self.leader_a)
        MonthEnrollment.objects.create(month=history, participant=self.reader_b)
        self.historical_book(history, self.reader, 400)
        self.historical_book(history, self.leader_a, 300)
        self.historical_book(history, self.reader_b, 500)
        second_history = ChallengeMonth.objects.create(
            group=self.group,
            name="Second Historical Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.COMPLETED,
        )
        MonthEnrollment.objects.create(month=second_history, participant=self.reader_b)
        self.historical_book(second_history, self.reader_b, 700)
        self.detail_url = self.month.get_absolute_url()
        self.hosts_url = reverse("challenge-host-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.floaters_url = reverse("challenge-floater-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.teams_url = reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.team_url = reverse("team-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.team.pk})

    def member(self, user, display_name, role=Membership.Role.MEMBER):
        return Membership.objects.create(group=self.group, user=user, display_name=display_name, role=role)

    def book(self, participant, title, base_pages, bonus_pages):
        return BookSubmission.objects.create(
            month=self.month,
            participant=participant,
            title=title,
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 9, 10),
            submitted_pages=base_pages,
            approved_pages=base_pages,
            bonus_pages=bonus_pages,
            status=BookSubmission.Status.APPROVED,
        )

    def historical_book(self, month, participant, pages):
        return BookSubmission.objects.create(
            month=month,
            participant=participant,
            title=f"Historical Book {participant.pk} {month.pk}",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=month.starts_on,
            submitted_pages=pages,
            approved_pages=pages,
            final_scored_pages=pages,
            status=BookSubmission.Status.APPROVED,
        )

    def test_challenge_summaries_link_to_sections_without_direct_management_controls(self):
        self.client.force_login(self.host_user)
        response = self.client.get(self.detail_url)
        self.assertContains(response, f'href="{self.hosts_url}"')
        self.assertContains(response, f'href="{self.floaters_url}"')
        self.assertContains(response, f'href="{self.teams_url}"')
        self.assertNotContains(response, "Manage Hosts")
        self.assertNotContains(response, "Manage Floaters")
        self.assertNotContains(response, ">Add Team</a>")

    def test_dedicated_pages_own_existing_management_controls(self):
        self.client.force_login(self.owner_user)
        hosts = self.client.get(self.hosts_url)
        self.assertContains(hosts, "Assign Host")
        self.client.force_login(self.host_user)
        floaters = self.client.get(self.floaters_url)
        self.assertContains(floaters, "Assign Floater")
        teams = self.client.get(self.teams_url)
        self.assertContains(teams, ">Add Team</a>")
        self.assertContains(teams, f'href="{self.team_url}"')
        self.assertContains(teams, "team-card-entry")
        self.assertNotContains(teams, "View Team")
        self.assertNotContains(teams, "member-chip")
        self.assertNotContains(teams, "Aaron Reader")
        self.assertNotContains(teams, "Beth Reader")
        self.assertContains(teams, "Team Leaders: Amy Leader, Zoe Leader")
        self.assertContains(teams, "Edit Team")
        self.assertContains(teams, "Manage Team Leaders")

    def test_host_can_view_hosts_but_cannot_manage_host_staffing(self):
        self.client.force_login(self.host_user)
        response = self.client.get(self.hosts_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Assign Host")
        self.assertEqual(self.client.post(self.hosts_url, {"membership": self.reader.pk}).status_code, 403)

    def test_team_detail_shows_leaders_current_roster_order_and_trustworthy_total(self):
        self.client.force_login(self.host_user)
        response = self.client.get(self.team_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.team.name)
        self.assertContains(response, self.team.color)
        self.assertContains(response, "Displayed Team Total")
        self.assertContains(response, ">430<")
        self.assertEqual(
            [assignment.participant.display_name for assignment in response.context["roster"]],
            ["Amy Leader", "Zoe Leader", "Aaron Reader", "Beth Reader"],
        )
        self.assertEqual(
            [assignment.membership.display_name for assignment in response.context["current_leaders"]],
            ["Amy Leader", "Zoe Leader"],
        )
        self.assertEqual(response.context["logical_parent_url"], self.teams_url)
        reader_assignment = next(assignment for assignment in response.context["roster"] if assignment.participant_id == self.reader.pk)
        expected_planning = historical_reader_planning_data(month=self.month, participant_ids=[self.reader.pk])[self.reader.pk]
        self.assertEqual(reader_assignment.planning, expected_planning)
        self.assertEqual((reader_assignment.base_pages, reader_assignment.modifier_pages, reader_assignment.total_pages), (100, 20, 120))
        self.assertContains(response, reverse("participant-detail", kwargs={"group_slug": self.group.slug, "pk": self.reader.pk}))
        self.assertContains(response, "team-detail-desktop")
        self.assertContains(response, "team-detail-mobile")
        self.assertContains(response, "private-leader")
        self.assertEqual(TeamAssignment.objects.filter(month=self.month, team=self.team, ended_at__isnull=True).count(), 4)

    def test_show_bonuses_display_contract_is_local_and_preserves_all_data(self):
        self.client.force_login(self.host_user)
        submission = BookSubmission.objects.get(month=self.month, participant=self.reader)
        state_before = (
            submission.approved_pages,
            submission.bonus_pages,
            submission.final_scored_pages,
            TeamAssignment.objects.filter(month=self.month, ended_at__isnull=True).count(),
            MonthEnrollment.objects.filter(month=self.month, is_active=True).count(),
            self.month.status,
        )
        response = self.client.get(self.team_url)
        reader = next(assignment for assignment in response.context["roster"] if assignment.participant_id == self.reader.pk)
        self.assertEqual((reader.base_pages, reader.modifier_pages, reader.total_pages), (100, 20, 120))
        self.assertContains(response, 'id="show-team-bonuses" type="checkbox" checked')
        self.assertContains(response, "Show Bonuses")
        self.assertContains(response, 'data-base-total="350"')
        self.assertContains(response, 'data-bonus-total="430"')
        self.assertContains(response, 'data-base-pages="100" data-bonus-pages="20"')
        self.assertContains(response, "data-modifier-display")
        self.assertContains(response, "data-modifier-sort-heading")
        self.assertContains(response, "data-modifier-sort-option")
        self.assertContains(response, "<dt data-modifier-display>Modifier</dt>", html=True)
        self.assertContains(response, "Base + Modifier · Display only")
        client_script = Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
        script = client_script.read_text()
        self.assertIn("element.hidden = !bonusToggle.checked", script)
        self.assertIn("modifierSortOption.disabled = !bonusToggle.checked", script)
        self.assertIn("container.dataset.sortKey = \"total\"", script)
        self.assertIn('mobileSortField.value = "total"', script)
        submission.refresh_from_db()
        self.month.refresh_from_db()
        state_after = (
            submission.approved_pages,
            submission.bonus_pages,
            submission.final_scored_pages,
            TeamAssignment.objects.filter(month=self.month, ended_at__isnull=True).count(),
            MonthEnrollment.objects.filter(month=self.month, is_active=True).count(),
            self.month.status,
        )
        self.assertEqual(state_after, state_before)
        self.assertContains(self.client.get(self.teams_url), ">430<")
        self.assertContains(self.client.get(self.detail_url), ">430<")

    def test_team_detail_sorting_is_numeric_predictable_and_preserves_leader_priority(self):
        self.client.force_login(self.host_user)
        expectations = {
            ("reader", "asc"): ["Amy Leader", "Zoe Leader", "Aaron Reader", "Beth Reader"],
            ("reader", "desc"): ["Zoe Leader", "Amy Leader", "Beth Reader", "Aaron Reader"],
            ("average", "asc"): ["Amy Leader", "Zoe Leader", "Aaron Reader", "Beth Reader"],
            ("average", "desc"): ["Amy Leader", "Zoe Leader", "Beth Reader", "Aaron Reader"],
            ("last", "asc"): ["Amy Leader", "Zoe Leader", "Aaron Reader", "Beth Reader"],
            ("last", "desc"): ["Amy Leader", "Zoe Leader", "Beth Reader", "Aaron Reader"],
            ("completed", "asc"): ["Zoe Leader", "Amy Leader", "Aaron Reader", "Beth Reader"],
            ("completed", "desc"): ["Amy Leader", "Zoe Leader", "Beth Reader", "Aaron Reader"],
            ("base", "asc"): ["Zoe Leader", "Amy Leader", "Beth Reader", "Aaron Reader"],
            ("base", "desc"): ["Amy Leader", "Zoe Leader", "Aaron Reader", "Beth Reader"],
            ("modifier", "asc"): ["Amy Leader", "Zoe Leader", "Aaron Reader", "Beth Reader"],
            ("modifier", "desc"): ["Amy Leader", "Zoe Leader", "Beth Reader", "Aaron Reader"],
            ("total", "asc"): ["Zoe Leader", "Amy Leader", "Beth Reader", "Aaron Reader"],
            ("total", "desc"): ["Amy Leader", "Zoe Leader", "Aaron Reader", "Beth Reader"],
        }
        for (sort, direction), expected in expectations.items():
            with self.subTest(sort=sort, direction=direction):
                response = self.client.get(self.team_url, {"sort": sort, "direction": direction})
                self.assertEqual([item.participant.display_name for item in response.context["roster"]], expected)
                self.assertEqual(response.context["sort_key"], sort)
                self.assertEqual(response.context["sort_direction"], direction)
        average_desc = self.client.get(self.team_url, {"sort": "average", "direction": "desc"})
        self.assertEqual(average_desc.context["roster"][1].participant, self.leader_z)
        self.assertContains(average_desc, "team-mobile-sort")
        modifier_desc = self.client.get(self.team_url, {"sort": "modifier", "direction": "desc"})
        self.assertContains(modifier_desc, 'data-score-roster data-sort-key="modifier" data-sort-direction="desc"', count=2)
        self.assertContains(modifier_desc, 'value="modifier" data-modifier-sort-option selected')

    def test_ordinary_reader_gets_no_management_and_platform_override_keeps_access(self):
        self.month.team_standings_visibility = ChallengeMonth.CompetitionVisibility.HOSTS
        self.month.reader_scores_visibility = ChallengeMonth.CompetitionVisibility.HOSTS
        self.month.save(update_fields=["team_standings_visibility", "reader_scores_visibility"])
        self.client.force_login(self.reader_user)
        reader_response = self.client.get(self.team_url, {"sort": "total", "direction": "desc"})
        self.assertEqual(reader_response.status_code, 200)
        self.assertEqual(reader_response.context["sort_key"], "reader")
        self.assertNotContains(reader_response, "Edit Team")
        self.assertNotContains(reader_response, "Manage Team Leaders")
        self.assertNotContains(reader_response, "Remove from Team")
        self.assertNotContains(reader_response, "private-leader")
        self.assertNotContains(reader_response, "Avg Pages")
        self.assertNotContains(reader_response, ">Base</a>")
        self.assertNotContains(reader_response, "Show Bonuses")
        self.assertNotContains(reader_response, "data-show-team-bonuses")
        self.assertEqual(self.client.post(self.floaters_url, {"membership": self.reader.pk}).status_code, 403)
        self.client.force_login(self.platform_owner)
        platform_response = self.client.get(self.team_url)
        self.assertEqual(platform_response.status_code, 200)
        self.assertContains(platform_response, "Edit Team")
        self.assertFalse(Membership.objects.filter(user=self.platform_owner).exists())
        self.assertFalse(ChallengeStaffAssignment.objects.filter(membership__user=self.platform_owner).exists())
        self.assertFalse(MonthEnrollment.objects.filter(participant__user=self.platform_owner).exists())
