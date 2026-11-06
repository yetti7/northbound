from django.utils import timezone
from datetime import date
from importlib import import_module

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.operations.special import RunPython
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .models import (
    BookSubmission,
    ChallengeMonth,
    ChallengeStaffAssignment,
    Membership,
    ModifierProvenance,
    MonthEnrollment,
    ReadingGroup,
    Team,
    TeamAssignment,
)
from .permissions import can_view_reader_scores, can_view_team_standings


class CompetitionVisibilityMatrixTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.users = {
            key: (User.objects.create_superuser("visibility-platform") if key == "platform" else User.objects.create_user(f"visibility-{key}"))
            for key in ("owner", "moderator", "host", "leader", "same", "other", "floater", "member", "platform")
        }
        self.group = ReadingGroup.objects.create(name="Competition Visibility", slug="competition-visibility")
        self.members = {
            "owner": self.member("owner", Membership.Role.OWNER),
            "moderator": self.member("moderator", Membership.Role.MODERATOR, {"manage_months": True}),
            "host": self.member("host"),
            "leader": self.member("leader"),
            "same": self.member("same"),
            "other": self.member("other"),
            "floater": self.member("floater"),
            "member": self.member("member"),
        }
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Visible Competition",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            status=ChallengeMonth.Status.ACTIVE,
        )
        self.north = Team.objects.create(month=self.month, name="North", color="#112233")
        self.south = Team.objects.create(month=self.month, name="South", color="#445566")
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.members["host"], role=ChallengeStaffAssignment.Role.HOST)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.members["floater"], role=ChallengeStaffAssignment.Role.FLOATER)
        for key, team in (("leader", self.north), ("same", self.north), ("other", self.south)):
            MonthEnrollment.objects.create(month=self.month, participant=self.members[key])
            TeamAssignment.objects.create(month=self.month, participant=self.members[key], team=team)
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.members["leader"],
            team=self.north,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        )
        self.book(self.members["leader"], "Leader Score", 100, 10)
        self.book(self.members["same"], "Same Reader Score", 200, 20)
        self.book(self.members["other"], "Other Reader Score", 400, 40)

    def member(self, key, role=Membership.Role.MEMBER, overrides=None):
        return Membership.objects.create(
            group=self.group,
            user=self.users[key],
            role=role,
            display_name=key.title(),
            permission_overrides=overrides or {},
        )

    def book(self, participant, title, base, bonus):
        submission = BookSubmission.objects.create(
            month=self.month,
            participant=participant,
            title=title,
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 9, 10),
            submitted_pages=base,
            approved_pages=base,
            bonus_pages=bonus,
            final_scored_pages=base + bonus,
            status=BookSubmission.Status.APPROVED,
        )
        if bonus:
            ModifierProvenance.objects.create(
                month=self.month,
                participant=participant,
                submission=submission,
                source_type=ModifierProvenance.SourceType.LEGACY_MODIFIER,
                source_reference=f"visibility-test:{submission.pk}",
                source_label="Test Modifier",
                amount=bonus,
                effective_date=submission.completed_on,
            )
        return submission

    def set_levels(self, level):
        self.month.team_standings_visibility = level
        self.month.reader_scores_visibility = level
        self.month.save(update_fields=["team_standings_visibility", "reader_scores_visibility"])

    def test_challenge_card_statistics_role_and_lifecycle_matrix(self):
        url = reverse('month-list', kwargs={'group_slug': self.group.slug})
        # Everybody competition visibility must not bypass the card policy.
        self.set_levels(ChallengeMonth.CompetitionVisibility.EVERYBODY)
        for status in ChallengeMonth.Status.values:
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
            for role in self.users:
                with self.subTest(status=status, role=role):
                    self.client.force_login(self.users[role])
                    response = self.client.get(url, {'archive': '1'} if status == 'archived' else {})
                    self.assertEqual(response.status_code, 200)
                    cards = list(response.context['months'])
                    discoverable = status != 'draft' or role in {'platform', 'owner', 'moderator', 'host'}
                    self.assertEqual(bool(cards), discoverable)
                    visible = discoverable and (role == 'platform' or status in {'completed', 'archived'})
                    if visible:
                        self.assertContains(response, '<strong>770</strong> verified pages · 3 books', html=False)
                        self.assertEqual((cards[0].approved_pages, cards[0].approved_books), (770, 3))
                    else:
                        self.assertNotContains(response, 'verified pages ·')
                        self.assertNotContains(response, '<strong>770</strong>')
                        self.assertNotContains(response, '3 books')
                        for card in cards:
                            self.assertFalse(hasattr(card, 'approved_pages'))
                            self.assertFalse(hasattr(card, 'approved_books'))
                    if discoverable:
                        self.assertContains(response, self.month.get_absolute_url())
                    else:
                        self.assertNotContains(response, self.month.name)

    def test_challenge_card_statistics_do_not_require_enrollment(self):
        member = self.members['member']
        self.assertFalse(MonthEnrollment.objects.filter(month=self.month, participant=member).exists())
        url = reverse('month-list', kwargs={'group_slug': self.group.slug})
        self.client.force_login(self.users['member'])
        self.assertNotContains(self.client.get(url), 'verified pages ·')
        MonthEnrollment.objects.create(month=self.month, participant=member)
        self.assertNotContains(self.client.get(url), 'verified pages ·')
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='completed')
        self.assertContains(self.client.get(url), '<strong>770</strong> verified pages · 3 books')

    def test_challenge_listing_still_rejects_nonmembers_and_preserves_archive_navigation(self):
        outsider = get_user_model().objects.create_user('card-outsider')
        url = reverse('month-list', kwargs={'group_slug': self.group.slug})
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(url).status_code, 403)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='archived')
        self.client.force_login(self.users['member'])
        response = self.client.get(url)
        self.assertNotContains(response, self.month.name)
        self.assertContains(response, '?archive=1')
        self.assertContains(self.client.get(url, {'archive': '1'}), self.month.name)

    def test_completed_results_visible_to_nonparticipant_in_score_surfaces(self):
        self.assertFalse(MonthEnrollment.objects.filter(participant=self.members['member']).exists())
        self.assertFalse(TeamAssignment.objects.filter(participant=self.members['member']).exists())
        self.client.force_login(self.users['member'])
        for status in (ChallengeMonth.Status.COMPLETED, ChallengeMonth.Status.ARCHIVED):
            for level in ChallengeMonth.CompetitionVisibility.values:
                with self.subTest(status=status, level=level):
                    ChallengeMonth.objects.filter(pk=self.month.pk).update(
                        status=status, team_standings_visibility=level, reader_scores_visibility=level)
                    self.month.refresh_from_db()
                    for helper in (can_view_team_standings, can_view_reader_scores):
                        self.assertTrue(helper(self.users['member'], self.month, team=self.north))
                    listing = self.client.get(reverse('team-list', args=[self.group.slug, self.month.pk]))
                    self.assertContains(listing, '<strong>330</strong> verified pages')
                    self.assertContains(listing, '<strong>440</strong> verified pages')
                    detail = self.client.get(self.month.get_absolute_url())
                    self.assertEqual(len(detail.context['comparison_teams']), 2)
                    self.assertContains(detail, '<strong>330</strong>')
                    team_url = reverse('team-detail', args=[self.group.slug, self.month.pk, self.north.pk])
                    report = self.client.get(team_url, {'sort': 'total', 'direction': 'desc'})
                    self.assertEqual(report.context['sort_key'], 'total')
                    self.assertEqual(report.context['team_total'], 330)
                    self.assertContains(report, '<td>200</td>')
                    self.assertContains(report, '<td>20</td>')
                    self.assertContains(report, '<strong>220</strong>')
                    self.assertContains(report, '<dt>Base</dt><dd>200</dd>', html=True)
                    filtered = self.client.get(team_url, {'from': '2026-09-11', 'to': '2026-09-30'})
                    self.assertEqual(filtered.context['team_total'], 0)
                    self.assertContains(filtered, '<strong>0</strong>')
                    self.assertEqual((report.context['month'].team_standings_visibility,
                                      report.context['month'].reader_scores_visibility), (level, level))

    def test_completed_results_preserve_group_and_private_information_boundaries(self):
        outsider = get_user_model().objects.create_user('completed-outsider')
        inactive_user = get_user_model().objects.create_user('completed-inactive')
        Membership.objects.create(group=self.group, user=inactive_user, display_name='Inactive', is_active=False)
        team_url = reverse('team-detail', args=[self.group.slug, self.month.pk, self.north.pk])
        for status in ('completed', 'archived'):
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
            self.month.refresh_from_db()
            for user in (AnonymousUser(), outsider, inactive_user):
                with self.subTest(status=status, user=str(user)):
                    for helper in (can_view_team_standings, can_view_reader_scores):
                        self.assertFalse(helper(user, self.month, team=self.north))
                    self.client.logout()
                    if user.is_authenticated:
                        self.client.force_login(user)
                    for url in (team_url, self.month.get_absolute_url(),
                                reverse('team-list', args=[self.group.slug, self.month.pk])):
                        response = self.client.get(url)
                        self.assertEqual(response.status_code, 403 if user.is_authenticated else 302)
                        self.assertNotContains(response, '<strong>330</strong>', status_code=response.status_code)
            self.assertTrue(can_view_reader_scores(self.users['platform'], self.month, team=self.north))
            self.client.force_login(self.users['member'])
            report = self.client.get(team_url, {'show_average': '1', 'show_discord': '1', 'show_last': '1'})
            for flag in ('can_view_planning_data', 'can_filter_progress', 'can_filter_discord', 'can_remove_roster'):
                self.assertFalse(report.context[flag])
            self.assertNotContains(report, 'Average Pages')
            self.assertNotContains(report, 'Discord Username')
            submission = self.month.submissions.first()
            urls = [
                reverse('submission-review', args=[self.group.slug, self.month.pk, submission.pk]),
                reverse('challenge-participant-detail', args=[self.group.slug, self.month.pk, self.members['same'].pk]),
                reverse('challenge-visibility-settings', args=[self.group.slug, self.month.pk]),
                reverse('config-user-list'),
            ]
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_precompletion_audiences_and_team_leader_scope_remain_enforced(self):
        for status in ('draft', 'upcoming', 'active', 'finalizing'):
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
            self.month.refresh_from_db()
            self.set_levels(ChallengeMonth.CompetitionVisibility.HOSTS)
            for helper in (can_view_team_standings, can_view_reader_scores):
                self.assertFalse(helper(self.users['member'], self.month, team=self.north))
            self.set_levels(ChallengeMonth.CompetitionVisibility.HOSTS_TEAM_LEADERS)
            for helper in (can_view_team_standings, can_view_reader_scores):
                self.assertTrue(helper(self.users['leader'], self.month, team=self.north))
                self.assertFalse(helper(self.users['leader'], self.month, team=self.south))
            if status in ('active', 'finalizing'):
                self.client.force_login(self.users['leader'])
                response = self.client.get(reverse('team-detail', args=[self.group.slug, self.month.pk, self.south.pk]))
                self.assertIsNone(response.context['team_total'])
                self.assertNotContains(response, '<strong>440</strong>')
                self.assertNotContains(response, '<td>400</td>')

    def test_every_level_uses_role_aware_team_scope_for_both_domains(self):
        level = ChallengeMonth.CompetitionVisibility
        staff = {"platform", "owner", "moderator", "host"}
        for visibility in level.values:
            self.set_levels(visibility)
            for key, user in self.users.items():
                for team in (self.north, self.south):
                    expected = key in staff
                    if visibility == level.EVERYBODY:
                        expected = True
                    elif visibility == level.HOSTS_FLOATERS and key == "floater":
                        expected = True
                    elif visibility == level.HOSTS_TEAM_LEADERS and (key == "floater" or (key == "leader" and team == self.north)):
                        expected = True
                    elif visibility == level.TEAM_MEMBERS:
                        expected = expected or (key in {"leader", "same"} and team == self.north) or (key == "other" and team == self.south)
                    with self.subTest(visibility=visibility, user=key, team=team.name):
                        self.assertEqual(can_view_team_standings(user, self.month, team=team), expected)
                        self.assertEqual(can_view_reader_scores(user, self.month, team=team), expected)

    def test_combined_audience_uses_current_floaters_and_preserves_stored_value(self):
        self.set_levels('hosts_leaders')
        for helper in (can_view_team_standings, can_view_reader_scores):
            self.assertTrue(helper(self.users['floater'], self.month))
            self.assertFalse(helper(self.users['leader'], self.month))
        self.client.force_login(self.users['owner'])
        settings_url = reverse('challenge-settings', kwargs={'group_slug': self.group.slug, 'month_pk': self.month.pk})
        self.assertContains(self.client.get(settings_url), 'Hosts + Floaters + Team Leaders', count=2)
        assignment = ChallengeStaffAssignment.objects.get(month=self.month, membership=self.members['floater'])
        assignment.ended_at = timezone.now()
        assignment.save(update_fields=['ended_at'])
        for helper in (can_view_team_standings, can_view_reader_scores):
            self.assertFalse(helper(self.users['floater'], self.month, team=self.north))
        self.month.refresh_from_db()
        self.assertEqual(self.month.team_standings_visibility, 'hosts_leaders')
        self.assertEqual(self.month.reader_scores_visibility, 'hosts_leaders')

    def test_team_scoped_surfaces_and_hidden_sort_do_not_leak(self):
        self.set_levels(ChallengeMonth.CompetitionVisibility.TEAM_MEMBERS)
        north_url = reverse("team-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.north.pk})
        south_url = reverse("team-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.south.pk})
        self.client.force_login(self.users["same"])
        north = self.client.get(north_url)
        self.assertContains(north, "Show Bonuses")
        self.assertContains(north, ">200<")
        self.assertNotContains(north, "Avg Pages")
        south = self.client.get(south_url, {"sort": "total", "direction": "desc"})
        self.assertEqual(south.context["sort_key"], "reader")
        self.assertIsNone(south.context["team_total"])
        self.assertNotContains(south, "Show Bonuses")
        self.assertNotContains(south, "data-base-pages")
        self.assertNotContains(south, ">Base</a>")
        self.assertFalse(hasattr(south.context["roster"][0], "base_pages"))
        detail = self.client.get(self.month.get_absolute_url())
        self.assertContains(detail, ">330<")
        self.assertNotContains(detail, ">440<")
        teams = self.client.get(reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertContains(teams, ">330<")
        self.assertNotContains(teams, ">440<")

    def test_floater_has_no_implicit_visibility_and_everybody_includes_members(self):
        self.set_levels(ChallengeMonth.CompetitionVisibility.HOSTS)
        self.members["member"].permission_overrides = {"view_hidden_stats": True}
        self.members["member"].save(update_fields=["permission_overrides"])
        for key in ("floater", "member"):
            self.assertFalse(can_view_team_standings(self.users[key], self.month, team=self.north))
            self.assertFalse(can_view_reader_scores(self.users[key], self.month, team=self.north))
        self.set_levels(ChallengeMonth.CompetitionVisibility.EVERYBODY)
        for key in ("floater", "member"):
            self.assertTrue(can_view_team_standings(self.users[key], self.month, team=self.north))
            self.assertTrue(can_view_reader_scores(self.users[key], self.month, team=self.north))

    def test_team_comparison_link_preserves_existing_standings_visibility(self):
        team_url = reverse(
            "team-list",
            kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk},
        )
        self.set_levels(ChallengeMonth.CompetitionVisibility.HOSTS)
        self.client.force_login(self.users["member"])
        hidden = self.client.get(self.month.get_absolute_url())
        self.assertNotContains(hidden, "Team Comparison")
        self.assertNotContains(hidden, "team-comparison-link")

        self.client.force_login(self.users["host"])
        visible = self.client.get(self.month.get_absolute_url())
        self.assertContains(visible, "Team Comparison")
        self.assertContains(
            visible,
            f'<a class="bar-chart team-comparison-link" href="{team_url}"',
        )
        self.assertContains(visible, ">330<")
        self.assertContains(visible, ">440<")

    def test_configuration_authority_settings_navigation_and_legacy_redirect(self):
        settings_url = reverse("challenge-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        visibility_url = reverse("challenge-visibility-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        old_url = reverse("team-stats-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        for key in ("owner", "moderator", "host", "platform"):
            self.client.force_login(self.users[key])
            self.assertEqual(self.client.get(visibility_url).status_code, 200)
        for key in ("leader", "same", "other", "floater", "member"):
            self.client.force_login(self.users[key])
            self.assertEqual(self.client.get(visibility_url).status_code, 403)
        self.client.force_login(self.users["owner"])
        page = self.client.get(settings_url)
        self.assertContains(page, "Team standings")
        self.assertContains(page, "Reader scores")
        self.assertContains(page, f'href="{visibility_url}"')
        self.assertRedirects(self.client.get(old_url), visibility_url)
        editor = self.client.get(visibility_url)
        self.assertEqual(editor.context["logical_parent_url"], settings_url)
        self.assertContains(editor, f'href="{settings_url}">Cancel</a>')
        for label in ("Hosts only", "Hosts + Floaters", "Hosts + Floaters + Team Leaders", "Team Members", "Everybody"):
            self.assertContains(editor, f">{label}</option>", count=2)
        self.assertNotContains(editor, "Owner only")
        self.assertNotContains(editor, "Moderator + Owner")
        self.assertContains(editor, "administrative oversight separately")
        detail = self.client.get(self.month.get_absolute_url())
        self.assertNotContains(detail, ">Visibility</a>")

    def test_self_submission_results_remain_visible_when_competition_is_private(self):
        self.set_levels(ChallengeMonth.CompetitionVisibility.HOSTS)
        self.client.force_login(self.users["same"])
        response = self.client.get(self.month.get_absolute_url())
        self.assertContains(response, "Same Reader Score")
        self.assertContains(response, ">200<")
        self.assertContains(response, ">20<")
        self.assertContains(response, ">220<")


class CompetitionVisibilityMigrationTests(TransactionTestCase):
    def tearDown(self):
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
        finally:
            super().tearDown()

    migrate_from = ("core", "0040_challenge_creation_handoff")
    migrate_to = ("core", "0041_challenge_competition_visibility")

    def test_legacy_values_map_deterministically_and_new_default_is_private(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Group = old_apps.get_model("core", "ReadingGroup")
        Month = old_apps.get_model("core", "ChallengeMonth")
        group = Group.objects.create(name="Migration Visibility", slug="migration-visibility")
        ids = {
            legacy: Month.objects.create(group=group, name=f"Legacy {legacy}", team_stats_visibility=legacy).pk
            for legacy in ("owner", "staff", "everyone")
        }
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        apps = executor.loader.project_state([self.migrate_to]).apps
        Month = apps.get_model("core", "ChallengeMonth")
        expected = {"owner": "owner", "staff": "moderator", "everyone": "everybody"}
        for legacy, pk in ids.items():
            month = Month.objects.get(pk=pk)
            self.assertEqual(month.team_standings_visibility, expected[legacy])
            self.assertEqual(month.reader_scores_visibility, expected[legacy])
        new_month = Month.objects.create(group_id=group.pk, name="New Private Default")
        self.assertEqual(new_month.team_standings_visibility, "owner")
        self.assertEqual(new_month.reader_scores_visibility, "owner")


class ChallengeRoleVisibilityMigrationTests(TransactionTestCase):
    def tearDown(self):
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
        finally:
            super().tearDown()

    migrate_from = ("core", "0041_challenge_competition_visibility")
    migrate_to = ("core", "0042_challenge_role_visibility_audiences")

    def test_both_columns_are_wide_enough_before_data_conversion(self):
        module = import_module("core.migrations.0042_challenge_role_visibility_audiences")
        state = MigrationExecutor(connection).loader.project_state([self.migrate_from])
        for operation in module.Migration.operations:
            if isinstance(operation, RunPython):
                Month = state.apps.get_model("core", "ChallengeMonth")
                for name in ("team_standings_visibility", "reader_scores_visibility"):
                    field = Month._meta.get_field(name)
                    self.assertEqual(field.max_length, 14)
                    values = set(module.OLD_TO_CHALLENGE_ROLE.values()) | {value for value, _ in field.choices}
                    self.assertTrue(all(len(value) <= field.max_length for value in values))
            operation.state_forwards("core", state)

    def test_old_audiences_map_restrictively_to_challenge_roles(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Group = old_apps.get_model("core", "ReadingGroup")
        Month = old_apps.get_model("core", "ChallengeMonth")
        group = Group.objects.create(name="Role Audience Migration", slug="role-audience-migration")
        old_values = ("nobody", "owner", "moderator", "team_leader", "team_members", "everybody")
        ids = {
            value: Month.objects.create(
                group=group,
                name=f"Old {value}",
                team_standings_visibility=value,
                reader_scores_visibility=value,
            ).pk
            for value in old_values
        }
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        apps = executor.loader.project_state([self.migrate_to]).apps
        Month = apps.get_model("core", "ChallengeMonth")
        expected = {
            "nobody": "hosts",
            "owner": "hosts",
            "moderator": "hosts",
            "team_leader": "hosts_leaders",
            "team_members": "team_members",
            "everybody": "everybody",
        }
        for old_value, pk in ids.items():
            month = Month.objects.get(pk=pk)
            self.assertEqual(month.team_standings_visibility, expected[old_value])
            self.assertEqual(month.reader_scores_visibility, expected[old_value])
        new_month = Month.objects.create(group_id=group.pk, name="New Hosts Default")
        self.assertEqual(new_month.team_standings_visibility, "hosts")
        self.assertEqual(new_month.reader_scores_visibility, "hosts")
        floater_month = Month.objects.create(
            group_id=group.pk, name="Floater Audience",
            team_standings_visibility="hosts_floaters", reader_scores_visibility="hosts_floaters",
        )
        # Reverse data conversion must precede shrinking either column to 12.
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        PreviousMonth = executor.loader.project_state([self.migrate_from]).apps.get_model("core", "ChallengeMonth")
        for name in ("team_standings_visibility", "reader_scores_visibility"):
            self.assertEqual(PreviousMonth._meta.get_field(name).max_length, 12)
            self.assertEqual(getattr(PreviousMonth.objects.get(pk=ids["team_leader"]), name), "team_leader")
            self.assertEqual(getattr(PreviousMonth.objects.get(pk=floater_month.pk), name), "nobody")
