from datetime import date

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .models import (
    BookSubmission,
    ChallengeMonth,
    ChallengeStaffAssignment,
    Membership,
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
        return BookSubmission.objects.create(
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

    def set_levels(self, level):
        self.month.team_standings_visibility = level
        self.month.reader_scores_visibility = level
        self.month.save(update_fields=["team_standings_visibility", "reader_scores_visibility"])

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
                    elif visibility == level.HOSTS_TEAM_LEADERS and key == "leader" and team == self.north:
                        expected = True
                    elif visibility == level.TEAM_MEMBERS:
                        expected = expected or (key in {"leader", "same"} and team == self.north) or (key == "other" and team == self.south)
                    with self.subTest(visibility=visibility, user=key, team=team.name):
                        self.assertEqual(can_view_team_standings(user, self.month, team=team), expected)
                        self.assertEqual(can_view_reader_scores(user, self.month, team=team), expected)

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
        self.assertContains(page, "Team Standings")
        self.assertContains(page, "Reader Scores")
        self.assertContains(page, f'href="{visibility_url}">Manage Visibility</a>')
        self.assertRedirects(self.client.get(old_url), visibility_url)
        editor = self.client.get(visibility_url)
        self.assertEqual(editor.context["logical_parent_url"], settings_url)
        self.assertContains(editor, f'href="{settings_url}">Cancel</a>')
        for label in ("Hosts only", "Hosts + Floaters", "Hosts + Team Leaders", "Team Members", "Everybody"):
            self.assertContains(editor, label, count=2)
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
    migrate_from = ("core", "0041_challenge_competition_visibility")
    migrate_to = ("core", "0042_challenge_role_visibility_audiences")

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
