from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership, ModifierProvenance, MonthEnrollment, ReadingGroup, Team, TeamAssignment, UserProfile
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
        submission = BookSubmission.objects.create(
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
        if bonus_pages:
            ModifierProvenance.objects.create(
                month=self.month,
                participant=participant,
                submission=submission,
                source_type=ModifierProvenance.SourceType.LEGACY_MODIFIER,
                source_reference=f"team-planning-test:{submission.pk}",
                source_label="Test Modifier",
                amount=bonus_pages,
                effective_date=submission.completed_on,
            )
        return submission

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
        self.assertNotContains(teams, "hierarchy-reader")
        self.assertNotContains(teams, "hierarchy-reader-b")
        self.assertContains(teams, "Team Leaders: hierarchy-leader-a, hierarchy-leader-z")
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
        self.assertContains(response, "Team Total")
        self.assertContains(response, ">430<")
        self.assertEqual(
            [assignment.participant.user.username for assignment in response.context["roster"]],
            ["hierarchy-leader-a", "hierarchy-leader-z", "hierarchy-reader", "hierarchy-reader-b"],
        )
        self.assertEqual(
            [assignment.membership.user.username for assignment in response.context["current_leaders"]],
            ["hierarchy-leader-a", "hierarchy-leader-z"],
        )
        self.assertEqual(response.context["logical_parent_url"], self.teams_url)
        reader_assignment = next(assignment for assignment in response.context["roster"] if assignment.participant_id == self.reader.pk)
        expected_planning = historical_reader_planning_data(month=self.month, participant_ids=[self.reader.pk])[self.reader.pk]
        self.assertEqual(reader_assignment.planning, expected_planning)
        self.assertEqual((reader_assignment.base_pages, reader_assignment.modifier_pages, reader_assignment.total_pages), (100, 20, 120))
        self.assertContains(response, reverse("challenge-participant-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.reader.pk}))
        self.assertContains(response, "team-detail-desktop")
        self.assertContains(response, "team-detail-mobile")
        self.assertContains(response, 'data-discord="private-leader"')
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
        self.assertContains(response, 'type="checkbox" name="bonuses" value="1" checked')
        self.assertContains(response, "Show Bonuses")
        self.assertContains(response, "Filtered roster · Base + Modifier")
        base_only = self.client.get(self.team_url, {"bonuses": "0"})
        self.assertNotContains(base_only, "<th>Modifier</th>", html=True)
        self.assertContains(base_only, "Filtered roster · Base only")
        self.assertEqual(base_only.context["roster_total"], 350)
        self.assertEqual(base_only.context["team_total"], 430)
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

    def test_team_detail_numeric_sorting_ignores_roles_and_default_preserves_priority(self):
        self.client.force_login(self.host_user)
        expectations = {
            ("reader", "asc"): ["hierarchy-leader-a", "hierarchy-leader-z", "hierarchy-reader", "hierarchy-reader-b"],
            ("reader", "desc"): ["hierarchy-leader-z", "hierarchy-leader-a", "hierarchy-reader-b", "hierarchy-reader"],
            ("base", "asc"): ['hierarchy-leader-z', 'hierarchy-reader-b', 'hierarchy-reader', 'hierarchy-leader-a'],
            ("base", "desc"): ['hierarchy-leader-a', 'hierarchy-reader', 'hierarchy-reader-b', 'hierarchy-leader-z'],
            ("modifier", "asc"): ['hierarchy-leader-a', 'hierarchy-leader-z', 'hierarchy-reader', 'hierarchy-reader-b'],
            ("modifier", "desc"): ['hierarchy-reader-b', 'hierarchy-reader', 'hierarchy-leader-a', 'hierarchy-leader-z'],
            ("total", "asc"): ['hierarchy-leader-z', 'hierarchy-reader-b', 'hierarchy-reader', 'hierarchy-leader-a'],
            ("total", "desc"): ['hierarchy-leader-a', 'hierarchy-reader', 'hierarchy-reader-b', 'hierarchy-leader-z'],
        }
        for (sort, direction), expected in expectations.items():
            with self.subTest(sort=sort, direction=direction):
                response = self.client.get(self.team_url, {"sort": sort, "direction": direction})
                self.assertEqual([item.participant.user.username for item in response.context["roster"]], expected)
                self.assertEqual(response.context["sort_key"], sort)
                self.assertEqual(response.context["sort_direction"], direction)
        response = self.client.get(self.team_url)
        self.assertContains(response, "team-roster-sort")
        modifier_desc = self.client.get(self.team_url, {"sort": "modifier", "direction": "desc"})
        self.assertEqual(modifier_desc.context["sort_key"], "modifier")
        self.assertContains(modifier_desc, 'value="modifier" selected')

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

    def test_numeric_progress_sorts_all_roles_and_preserves_missing_last(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        self.client.force_login(self.host_user)
        readers=[self.leader_a,self.leader_z,self.reader,self.reader_b]
        # Real view sorting with isolated progress-source fixtures: Reader beats Leaders.
        from .models import MonthTheme,BotmBook
        for i in range(2):
            MonthTheme.objects.create(month=self.month,name='Sort Theme '+str(i),starts_on=self.month.starts_on,ends_on=self.month.ends_on)
            BotmBook.objects.create(month=self.month,position=i+1,title_snapshot='Sort BOTM '+str(i),author_snapshot='Author',page_count_snapshot=100)
        theme_rows=[(self.reader.pk,1),(self.reader.pk,2),(self.leader_a.pk,1)]
        tbrs=[SimpleNamespace(enrollment=SimpleNamespace(participant_id=m.pk),books=SimpleNamespace(count=lambda n=n:n))
              for m,n in [(self.reader,4),(self.leader_a,2),(self.reader_b,4)]]
        tbr_rows=[(self.reader.pk,11),(self.reader.pk,12),(self.reader.pk,13),(self.leader_a.pk,21)]
        botm_rows=[(self.reader.pk,1),(self.reader.pk,2),(self.leader_a.pk,1)]
        with patch('core.views.ThemeClaim') as themes,patch('core.views.PersonalTBR') as lists,patch('core.views.PersonalTBRMatch') as tbr,patch('core.views.BotmMatch') as botm:
            themes.objects.filter.return_value.values_list.return_value=theme_rows
            lists.objects.filter.return_value.select_related.return_value.prefetch_related.return_value=tbrs
            tbr.objects.filter.return_value.values_list.return_value=tbr_rows
            botm.objects.filter.return_value.values_list.return_value=botm_rows
            for metric in ['themes','tbr','botm']:
                for direction in ['asc','desc']:
                    response=self.client.get(self.team_url,{'sort':metric,'direction':direction})
                    roster=response.context['roster']
                    if metric=='themes':values=[r.theme_completed for r in roster]
                    elif metric=='tbr':values=[r.tbr_completed/r.tbr_total for r in roster if r.tbr_total]
                    else:values=[r.botm_completed/r.botm_total for r in roster if r.botm_total]
                    self.assertEqual(values,sorted(values,reverse=direction=='desc'))
                    if metric=='tbr':self.assertIsNone(roster[-1].tbr_total)
        for direction in ['asc','desc']:
            response=self.client.get(self.team_url,{'sort':'role','direction':direction})
            roles=[r.is_team_leader for r in response.context['roster']]
            self.assertEqual(roles,sorted(roles,reverse=direction=='asc'))
        default=self.client.get(self.team_url).context['roster']
        self.assertEqual([r.is_team_leader for r in default],[True,True,False,False])

    def test_numeric_tie_uses_username_not_role_and_keeps_ids_stable(self):
        user=get_user_model().objects.create_user('aardvark-reader')
        member=self.member(user,'Reader')
        MonthEnrollment.objects.create(month=self.month,participant=member)
        TeamAssignment.objects.create(month=self.month,participant=member,team=self.team)
        self.client.force_login(self.host_user)
        for direction in ['asc','desc']:
            rows=self.client.get(self.team_url,{'sort':'base','direction':direction}).context['roster']
            zeros=[r.participant_id for r in rows if r.base_pages==0]
            self.assertEqual(zeros,[member.pk,self.leader_z.pk])
